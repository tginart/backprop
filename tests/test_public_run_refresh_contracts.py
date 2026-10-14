"""Public refresh state transitions compared with installed W&B0.30.0.

Architecture: independent reference/native processes -> real methods and service
fixtures -> portable values, fetch counts, errors and recovery assertions.
Main caller: base SDK CI. Raw full payloads remain in test-results so selected
field comparisons cannot conceal Foundry identity/index/account differences.
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


class PublicRunRefreshContracts(unittest.TestCase):
    def observe(self, scenario):
        """Run one declared scenario in each SDK's own interpreter and imports."""
        if not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try: importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 wheel is required')
        reference = reference_runtime()
        results = []
        for sdk in ('reference', 'native'):
            output = ROOT / f'test-results/public-run-refresh-{scenario}-{sdk}.json'
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            environment.update(PYTHONDONTWRITEBYTECODE='1',
                               PYTHONPATH=str(ROOT/'tests') if sdk == 'reference' else str(ROOT/'sdk'))
            result = run_observation([reference.python if sdk == 'reference' else sys.executable,
                '-B', str(ROOT/'tests/public_run_refresh_probe.py'), sdk, scenario], output,
                cwd=tempfile.gettempdir(), env=environment, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            results.append(json.loads(output.read_text())['observations'])
        return results

    def test_full_and_lazy_refresh_caching_match_installed_reference(self):
        for mode in ('full', 'lazy'):
            with self.subTest(mode=mode):
                reference, native = self.observe('cache-' + mode)
                self.assertEqual(native['trace'], reference['trace'])
                self.assertEqual([r['reads'] for r in native['trace']],
                                 [0, 0, int(mode == 'lazy'), int(mode == 'lazy'), 1, 1])
                self.assertEqual(native['final_config'], {'rate': 2})
                self.assertEqual([r['scope'] for r in native['backend_reads']],
                                 [r['scope'] for r in reference['backend_reads']])
                for result in native['payloads']:
                    self.assertEqual(result['projectId'], native['project_internal_id'])
                    self.assertIsInstance(result['projectId'], int)
                # All selected run fields have counterparts. W&B account
                # objects are outside the Foundry identity contract.
                self.assertEqual(set(reference['payloads'][-1]) - set(native['payloads'][-1]), {'user'})
                self.assertEqual(set(native['payloads'][-1]) - set(reference['payloads'][-1]), set())

    def test_missing_run_and_transport_failures_preserve_data_then_recover(self):
        for failure in ('missing-response', 'missing-project', 'missing-run', 'transport'):
            for mode in ('full', 'lazy'):
                with self.subTest(failure=failure, mode=mode):
                    reference, native = self.observe(failure + '-' + mode)
                    # Messages name backend-specific resources; compare exception
                    # class, unchanged cache and successful recovery separately.
                    for observation in (reference, native):
                        for row in observation['trace']: row.pop('message', None)
                    self.assertEqual(native['trace'], reference['trace'])
                    trace = native['trace']
                    expected = 'ConnectionError' if failure == 'transport' else 'RunNotFoundError'
                    self.assertEqual([trace[i]['error'] for i in (1, 3)], [expected, expected])
                    self.assertEqual(trace[2]['value']['config'], {'rate': 1})
                    self.assertEqual(trace[-1]['value']['config'], {'rate': 2})

    def test_unsaved_edit_survives_cached_load_and_forced_load_replaces_it(self):
        for mode in ('full', 'lazy'):
            with self.subTest(mode=mode):
                reference, native = self.observe('unsaved-edit-' + mode)
                self.assertEqual(native['trace'], reference['trace'])
                self.assertEqual([r['value']['config'] for r in native['trace']],
                                 [{'rate': 99}, {'rate': 1}])

    def test_returned_dictionary_is_detached_as_documented_foundry_repair(self):
        for mode in ('full', 'lazy'):
            with self.subTest(mode=mode):
                reference, native = self.observe('returned-edit-' + mode)
                self.assertEqual(reference['trace'][1]['value']['config'], {'rate': 99})
                self.assertEqual(native['trace'][1]['value']['config'], {'rate': 1})
                self.assertEqual(native['trace'][-1], reference['trace'][-1])
                self.assertEqual(native['final_config'], {'rate': 1})

    def test_lightweight_reference_payload_upgrades_to_the_same_full_values(self):
        reference, native = self.observe('lightweight-lazy')
        self.assertNotIn('config', reference['payloads'][0])
        self.assertEqual(native['payloads'][0]['config'], {'rate': 1})
        self.assertEqual([r['reads'] for r in reference['trace']], [0, 1, 0])
        self.assertEqual([r['reads'] for r in native['trace']], [0, 1, 0])
        self.assertEqual(native['trace'][1:], reference['trace'][1:])
        # Foundry reads a complete event snapshot even for listed lazy handles;
        # it does not claim to withhold fields to mimic GraphQL transfer size.
        self.assertEqual(native['final_config'], {'rate': 2})


if __name__ == '__main__': unittest.main()
