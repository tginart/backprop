"""Compare simultaneous Gym ownership with isolated pinned/native processes.

Architecture: paired actual run lifecycles -> controlled completion boundaries
or installed real encoders -> strict routing plus explicit upstream bug records.
Main callers: local contract checks and all five optional-Gym matrix jobs. Why
this exists: single-run videos cannot verify global/create_new ownership or
whether a recorder surviving reinit contaminates its replacement experiment.
"""
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


class SimultaneousGymContracts(unittest.TestCase):
    def observe(self, real):
        """Retain exact runtime/version errors and require every configured run."""
        library = os.environ.get('BACKPROP_GYM_LIBRARY') or ('gymnasium' if importlib.util.find_spec('gymnasium') else 'gym')
        if real:
            missing = [name for name in [library, 'numpy', 'moviepy', 'imageio_ffmpeg']
                       if not importlib.util.find_spec(name)]
            if missing:
                if os.environ.get('BACKPROP_GYM_LIBRARY'):
                    self.fail('Configured optional Gym dependencies are missing: ' + ', '.join(missing))
                self.skipTest('Actual optional Gym dependencies are unavailable: ' + ', '.join(missing))
        reference = reference_runtime()
        pair = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'framework-init-gym-simultaneous-{"real" if real else "boundary"}-{library}-{mode}.json'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
                       WANDB_DATA_DIR='/tmp/backprop-reference-data', BACKPROP_GYM_LIBRARY=library)
            result = run_observation([reference.python if mode == 'reference' else sys.executable, '-B',
                str(ROOT / 'tests/framework_init_gym_simultaneous_probe.py'), mode, 'real' if real else 'boundary'],
                output, cwd=tempfile.gettempdir(), env=env, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            pair.append(json.loads(output.read_text()))
        return pair

    def assert_native(self, actual, real):
        for name, case in actual['observations'].items():
            self.assertEqual(case['first_rewards'], [1.0], name)
            self.assertEqual(case['second_rewards'], [1.0], name)
            self.assertEqual(case['first_close'], {'value': None}, name)
            self.assertEqual(case['second_close'], {'value': None}, name)
            self.assertTrue(case['global_empty'], name)
            self.assertTrue(case['repeat_global_empty'], name)
            self.assertEqual(case['hooks_after_teardown'], 0, name)
            self.assertEqual(case['repeat_close'], {'value': None}, name)
            self.assertEqual(case['repeat_videos'], 1, name)
            expected = {'global_create_new': (2, 0), 'only_create_new': (0, 0), 'finish_previous': (0, 1)}[name]
            self.assertEqual((case['first_videos'], case['second_videos']), expected, name)
            self.assertEqual(case['global_id'], None if name == 'only_create_new' else
                             ('second-' if name == 'finish_previous' else 'first-') + name)
            if name == 'finish_previous':
                self.assertTrue(any('after its Backprop run finished' in item for item in case['warnings']))
            if real:
                self.assertEqual(len(case['media']), 2)
                self.assertEqual(len(case['repeat_media']), 1)
                for value in case['media'] + case['repeat_media']:
                    self.assertTrue(value['mp4_header'])
                    self.assertGreater(value['bytes'], 100)

    def assert_reference(self, expected, real):
        broken = real and (expected['version'].startswith('0.25') or
                          (expected['library'] == 'gymnasium' and int(expected['version'].split('.')[0]) >= 1))
        for name, case in expected['observations'].items():
            self.assertEqual(case['first_rewards'], [1.0])
            self.assertEqual(case['second_rewards'], [1.0])
            self.assertTrue(case['global_empty'])
            self.assertTrue(case['repeat_global_empty'])
            # Pinned teardown retains its process-wide Gym patch; native
            # restores owned methods explicitly. Retain both raw outcomes.
            self.assertEqual(case['hooks_after_teardown'], 1)
            if broken:
                self.assertEqual(case['first_close']['error'], 'AttributeError')
                self.assertEqual(case['second_close']['error'], 'AttributeError')
                self.assertEqual(case['repeat_close']['error'], 'AttributeError')
                self.assertEqual((case['first_videos'], case['second_videos'], case['repeat_videos']), (0, 0, 0))
                continue
            self.assertEqual(case['first_close'], {'value': None})
            self.assertEqual(case['second_close'], {'value': None})
            counts = {'global_create_new': (2, 0), 'only_create_new': (0, 0), 'finish_previous': (0, 2)}[name]
            self.assertEqual((case['first_videos'], case['second_videos']), counts)
            self.assertEqual(case['repeat_close'], {'value': None})
            self.assertEqual(case['repeat_videos'], 1)

    def test_pinned_simultaneous_completion_boundary_and_owner_repair(self):
        expected, actual = self.observe(False)
        for result in [expected, actual]:
            self.assertEqual(result['library'], 'gym')
            self.assertTrue(all(case['boundary_recorder_selected'] for case in result['observations'].values()))
        self.assert_native(actual, False)
        self.assert_reference(expected, False)
        for name in ['global_create_new', 'only_create_new']:
            source_case, native_case = expected['observations'][name], actual['observations'][name]
            self.assertEqual({key: value for key, value in source_case.items() if key != 'hooks_after_teardown'},
                             {key: value for key, value in native_case.items() if key != 'hooks_after_teardown'})
            self.assertEqual(source_case['hooks_after_teardown'], 1)
            self.assertEqual(native_case['hooks_after_teardown'], 0)
        # Pinned close uses the replacement global, so an unfinished recording
        # is misattributed after finish_previous. Native keeps its captured owner.
        self.assertEqual(expected['observations']['finish_previous']['second_videos'], 2)
        self.assertEqual(actual['observations']['finish_previous']['second_videos'], 1)

    def test_actual_gym_init_runtime_simultaneous_recorders_route_and_cleanup(self):
        expected, actual = self.observe(True)
        self.assert_native(actual, True)
        self.assert_reference(expected, True)


if __name__ == '__main__':
    unittest.main()
