"""Measure unfinished-run teardown in an actual kernel without hiding errors.

Architecture: installed real kernel -> two isolated public lifecycle cells ->
source/native state and immutable histories -> exact callback error evidence.
Main callers: lower/latest Linux/Windows notebook CI. Why this exists: prior
successful explicit-finish tests did not verify unfinished public teardown.
Native cleanup is retained as an intentional repair of measured source state.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from reference_runtime import reference_runtime
from framework_gap_process import run_observation

ROOT = Path(__file__).resolve().parents[1]


class UnfinishedNotebookContracts(unittest.TestCase):
    def observe(self, mode):
        """Require actual kernel dependencies and preserve every child output.

        Main caller: the paired test. Explicit mandatory CI dependency failures
        raise; a local absent dependency is an honest verification skip.
        """
        missing = [name for name in ['ipykernel', 'jupyter_client'] if not importlib.util.find_spec(name)]
        if missing:
            if os.environ.get('BACKPROP_REQUIRE_NOTEBOOK_KERNEL') == '1':
                self.fail('Required real kernel dependencies are missing: ' + ', '.join(missing))
            self.skipTest('Actual kernel dependencies are unavailable: ' + ', '.join(missing))
        reference = reference_runtime()
        output = ROOT / 'test-results' / f'framework-init-windows-unfinished-notebook-{mode}.json'
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
        paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
        environment.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
            WANDB_DATA_DIR=str(Path(tempfile.gettempdir()) / 'backprop-reference-data'), TERM='xterm')
        process = run_observation([reference.python if mode == 'reference' else sys.executable, '-B',
            str(ROOT / 'tests/framework_init_windows_notebook_driver.py'), mode, str(output)],
            output, cwd=tempfile.gettempdir(), env=environment, timeout=100)
        self.assertEqual(process.returncode, 0, process.stderr.decode(errors='replace'))
        data = json.loads(output.read_text())
        driver = json.loads(output.with_suffix('.driver.json').read_text())
        self.assertEqual(driver['execution_statuses'], ['ok', 'ok'])
        case = data['observation']
        none = {'returned_run': False, 'id': None, 'value': None}
        for key in ['first_log', 'teardown', 'repeat_log', 'repeat_finish', 'repeat_teardown']:
            self.assertEqual(case[key], none)
        self.assertEqual([row['unfinished_before'] for row in case['old_history_after_teardown']], [1])
        self.assertEqual([row['unfinished_repeat'] for row in case['repeat_history']], [3])
        self.assertEqual(case['unknown_requests'], [])
        self.assertEqual(case['workers_after_teardown'], [])
        self.assertEqual(case['workers_after_repeat'], [])
        self.assertTrue(case['global_empty_after_repeat'])
        for request in case['requests']:
            self.assertIn(request['path'], {'/sdk/otel/v1/metrics', '/sdk/otel/v1/logs'})
            self.assertEqual(mode, 'reference')
            self.assertEqual(request['method'], 'POST')
            self.assertIsNone(request['body'])
            self.assertIsNone(request['operation'])
        for suffix in ['.json', '.first-stage.json', '.iopub.json', '.shell.json', '.driver.json', '.stdout.log', '.stderr.log']:
            for secret in [b'b' * 40, b'c' * 40]:
                self.assertNotIn(secret, output.with_suffix(suffix).read_bytes())
        return case, json.loads(output.with_suffix('.iopub.json').read_text())

    def test_actual_unfinished_teardown_callbacks_errors_and_repeat_session(self):
        pair = {}
        for mode in ['reference', 'native']:
            with self.subTest(mode=mode):
                pair[mode] = self.observe(mode)
        if len(pair) != 2:
            return
        source, source_messages = pair['reference']
        native, native_messages = pair['native']
        self.assertTrue(source['previous_global_after_teardown'])
        self.assertFalse(source['previous_closed_after_teardown'])
        self.assertEqual(source['old_log_after_teardown'], {'error': 'AlreadyJoinedError',
            'message': 'Cannot schedule tasks after join(). Did you call wandb.teardown()?', 'cause': None})
        for stage in ['callbacks_before_teardown', 'callbacks_after_teardown', 'callbacks_at_repeat_start', 'callbacks_after_repeat']:
            self.assertEqual(source[stage], {'pre_run_cell': ['_WandbInit._pre_run_cell_hook'],
                                           'post_run_cell': ['_WandbInit._post_run_cell_hook']})
            self.assertEqual(native[stage], {'pre_run_cell': [], 'post_run_cell': []})
        self.assertFalse(native['previous_global_after_teardown'])
        self.assertTrue(native['previous_closed_after_teardown'])
        self.assertEqual(native['old_log_after_teardown'], {'error': 'UsageError',
            'message': 'Run (unfinished) is finished. The call to `log` will be ignored. Please make sure that you are using an active run.', 'cause': None})
        source_text = json.dumps(source_messages)
        self.assertIn('AlreadyJoinedError', source_text)
        self.assertIn('_post_run_cell_hook', source_text)
        self.assertIn('_pre_run_cell_hook', source_text)
        self.assertFalse(any(message['type'] == 'error' for message in native_messages))
        self.assertNotIn('Error in callback', json.dumps(native_messages))


if __name__ == '__main__':
    unittest.main()
