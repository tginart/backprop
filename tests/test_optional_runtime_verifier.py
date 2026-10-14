"""Verify that missing coverage cannot become a passing runtime gate.

Architecture: tiny local unittest targets -> strict verifier -> assert failures,
skips, empty targets and import errors remain failures. These test the verifier,
not Plotly/Polars/RE2 behavior, and require no optional packages.
Main callers: ordinary unittest discovery before publishing verification tooling.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'tools/verify_optional_runtimes.py'
SPEC = importlib.util.spec_from_file_location('backprop_optional_runtime_verifier', PATH)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class RuntimeVerifierGuards(unittest.TestCase):
    def run_case(self, operation):
        """Run one owned synthetic case without registering it for discovery."""
        module = ModuleType('backprop_verifier_fixture')
        module.Case = type('Case', (unittest.TestCase,), {'test_gate': operation})
        with patch.dict(sys.modules, {module.__name__: module}):
            return verifier.run_selected_tests([module.__name__ + '.Case'], io.StringIO())

    def test_actual_pass_and_failure_produce_different_gate_results(self):
        passed = self.run_case(lambda case: case.assertTrue(True))
        self.assertEqual(passed['status'], 'passed')
        self.assertEqual(passed['tests_run'], 1)
        failed = self.run_case(lambda case: case.fail('Expected verifier fixture failure'))
        self.assertEqual(failed['status'], 'integration_failed')
        self.assertEqual(failed['failures'], 1)

    def test_skipped_required_test_cannot_pass(self):
        result = self.run_case(lambda case: case.skipTest('Provider unavailable'))
        self.assertEqual(result['status'], 'integration_failed')
        self.assertEqual(result['skipped'][0]['reason'], 'Provider unavailable')

    def test_expected_failure_cannot_pass_a_required_check(self):
        operation = unittest.expectedFailure(lambda case: case.fail('Known provider failure'))
        result = self.run_case(operation)
        self.assertEqual(result['status'], 'integration_failed')
        self.assertEqual(len(result['expected_failures']), 1)

    def test_empty_and_unloadable_targets_fail_before_execution(self):
        self.assertEqual(verifier.run_selected_tests([], io.StringIO())['status'], 'test_loading_failed')
        module = ModuleType('backprop_empty_verifier_fixture')
        with patch.dict(sys.modules, {module.__name__: module}):
            empty = verifier.run_selected_tests([module.__name__], io.StringIO())
            invalid = verifier.run_selected_tests([module.__name__ + '.missing_test'], io.StringIO())
        self.assertEqual(empty['status'], 'test_loading_failed')
        self.assertEqual(empty['empty_targets'], [module.__name__])
        self.assertEqual(invalid['status'], 'test_loading_failed')
        self.assertEqual(invalid['tests_run'], 0)

    def test_provider_import_initialization_failure_is_reported(self):
        with patch.object(verifier, 'PROVIDERS', {'missing_provider': 'missing-provider'}), \
                patch.object(verifier.importlib, 'import_module', side_effect=OSError('Missing binary dependency')):
            available, missing = verifier.installed_providers()
        self.assertEqual(available, {})
        self.assertEqual(missing, {'missing_provider': 'OSError'})


if __name__ == '__main__':
    unittest.main()
