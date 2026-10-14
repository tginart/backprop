"""Verify create_new adapter ownership without inventing absent runtime proof.

Architecture: actual offline SDKs -> source-backed dependency boundary or real
installed event writer -> raw delegate traces and histories -> explicit owner
assertions. Main callers: init contract CI. Boundary cases establish routing;
the independently skipped optional writer case is required for runtime closure.
"""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]


class FrameworkInitAdapterContracts(unittest.TestCase):
    def test_native_direct_run_manual_sync_and_explicit_global_binding_are_preserved(self):
        """Keep automatic ownership filtering out of explicit import callers.

        Main caller: portable routing regression. Only optional installation
        and decoding are replaced; real constructors, registration, polling and
        global binding remain intact. Actual decoded values are checked by the
        separately required installed EventFileWriter comparison.
        """
        import backprop
        from backprop.integration import tensorboard as tb
        backprop.teardown()
        tb.unpatch()
        settings = dict(mode='offline', sync_tensorboard=True, console='off', silent=True,
                        disable_git=True, disable_code=True, x_disable_stats=True)
        owners = []
        with tempfile.TemporaryDirectory(prefix='init-explicit-routing-') as directory, \
                patch.object(tb, 'patch'), \
                patch('backprop.settings_policy.validate_logging_settings', return_value=[]), \
                patch.object(tb, 'sync', side_effect=lambda run, *args, **kwargs: owners.append(run.id)):
            direct = backprop.Run(project='routing', entity='e', id='direct', dir=directory,
                                  settings=settings, _sink=backprop.MemorySink(), _deployment={})
            try:
                nonglobal = backprop.init(project='routing', entity='e', id='nonglobal', dir=directory,
                    reinit='create_new', settings=settings, _sink=backprop.MemorySink(), _deployment={})
                tb._directories.add(directory)
                owners.clear()
                direct.flush()
                self.assertEqual(owners, ['direct'])
                owners.clear()
                nonglobal.flush()
                self.assertEqual(owners, [])
                tb.sync(nonglobal, directory)
                self.assertEqual(owners, ['nonglobal'])
                owners.clear()
                backprop.run = nonglobal
                nonglobal.flush()
                self.assertEqual(owners, ['nonglobal'])
            finally:
                backprop.teardown()
                direct.finish()
                tb.unpatch()

    def observe(self, scenario):
        """Retain original source/native process diagnostics and JSON."""
        if not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 reference interpreter is unavailable')
        reference = reference_runtime()
        if scenario == 'writer':
            if not importlib.util.find_spec('tensorboard'):
                self.skipTest('Actual native optional TensorBoard runtime is unavailable')
            query = subprocess.run([reference.python, '-I', '-c',
                'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("tensorboard") else 3)'],
                capture_output=True, timeout=30)
            if query.returncode == 3:
                self.skipTest('Actual pinned-reference optional TensorBoard runtime is unavailable')
            self.assertEqual(query.returncode, 0, query.stderr.decode(errors='replace'))
        pair = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'framework-init-adapter-{scenario}-{mode}.json'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
                       WANDB_DATA_DIR='/tmp/backprop-reference-data')
            result = run_observation([reference.python if mode == 'reference' else sys.executable, '-B',
                str(ROOT / 'tests/framework_init_adapter_probe.py'), mode, scenario], output,
                cwd=tempfile.gettempdir(), env=env, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            pair.append(json.loads(output.read_text())['observations'])
        return pair

    def test_source_notification_and_actual_run_poll_owner_boundary(self):
        expected, actual = self.observe('boundary')
        for name in ('True', 'False', 'None'):
            self.assertEqual(expected[name]['automatic_owners'], ['first'] if name != 'None' else [])
            self.assertEqual(expected[name]['automatic_owners'], actual[name]['automatic_owners'], name)
            self.assertEqual(expected[name]['global_id'], actual[name]['global_id'])
            self.assertTrue(expected[name]['global_empty'])
            self.assertTrue(actual[name]['global_empty'])

    def test_actual_tensorboard_writer_routes_only_to_bound_global_run(self):
        expected, actual = self.observe('writer')
        self.assertEqual(expected, actual)
        for name in ('True', 'False', 'None'):
            self.assertEqual(expected[name]['first_values'], [.75] if name != 'None' else [])
            self.assertEqual(expected[name]['second_values'], [])


if __name__ == '__main__':
    unittest.main()
