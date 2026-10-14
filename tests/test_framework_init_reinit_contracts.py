"""Compare actual offline reinit and installed IPython shell policy.

Architecture: paired isolated SDKs -> real selected shell -> active handle and
history observations -> exact branch assertions. Main callers: init contracts
CI. The Jupyter case verifies real IPython's configured detection and policy;
it does not claim an authenticated notebook server or hosted provider runtime.
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


class FrameworkInitReinitContracts(unittest.TestCase):
    def observe(self, shell):
        """Retain mandatory JSON and diagnostics from independent runtimes."""
        if shell != 'python' and not importlib.util.find_spec('IPython'):
            self.skipTest('Actual optional IPython runtime is unavailable')
        if not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 reference interpreter is unavailable')
        reference = reference_runtime()
        pair = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'framework-init-reinit-{shell}-{mode}.json'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
                       WANDB_DATA_DIR='/tmp/backprop-reference-data')
            result = run_observation([reference.python if mode == 'reference' else sys.executable, '-B',
                str(ROOT / 'tests/framework_init_reinit_probe.py'), mode, shell], output,
                cwd=tempfile.gettempdir(), env=env, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            pair.append(json.loads(output.read_text()))
        return pair

    def check_policy(self, shell):
        expected, actual = self.observe(shell)
        self.assertEqual(expected['detection'], actual['detection'])
        self.assertEqual(expected['detection']['in_notebook'], shell != 'python')
        self.assertEqual(expected['detection']['in_jupyter'], shell == 'jupyter_detection')
        for name, source in expected['observations'].items():
            native = actual['observations'][name]
            if name.startswith('invalid_'):
                # Pydantic machinery is target NO; native UsageError is the
                # declared settings validation equivalent. Original text and
                # classes remain untouched in each observation artifact.
                if name == 'invalid_config':
                    self.assertEqual(source['error'], 'TypeError')
                    self.assertEqual(native, source)
                else:
                    field = name.removeprefix('invalid_')
                    value = 'not-a-mode' if field == 'mode' else 'not-a-policy'
                    self.assertEqual(source['error'], 'ValidationError')
                    self.assertIn('type=literal_error', source['message'])
                    self.assertEqual(native['error'], 'UsageError')
                    self.assertEqual(native['message'], f'Invalid Settings.{field}: {value!r}')
                for case in (source, native):
                    self.assertTrue(case['previous_global'])
                    self.assertEqual(case['previous_log'], {'value': None})
                    self.assertEqual(case['new_transactions'], 0)
                continue
            # Errors from calling a finished handle remain raw in artifacts;
            # compare the exact success/failure branches and all actual values.
            for call in ('first_log', 'second_log'):
                if call not in source:
                    continue
                self.assertEqual('error' in source[call], 'error' in native[call], (shell, name, call))
                if 'error' in source[call]:
                    self.assertEqual(source[call]['error'], 'UsageError')
                    self.assertEqual(native[call], source[call])
            portable = lambda row: {key: value for key, value in row.items() if key not in ('first_log', 'second_log')}
            self.assertEqual(portable(source), portable(native), (shell, name))

    def test_plain_python_default_and_explicit_reinit_routing(self):
        self.check_policy('python')

    def test_actual_terminal_ipython_default_and_explicit_reinit_routing(self):
        self.check_policy('terminal')

    def test_actual_ipython_jupyter_detection_default_reinit_routing(self):
        self.check_policy('jupyter_detection')


if __name__ == '__main__':
    unittest.main()
