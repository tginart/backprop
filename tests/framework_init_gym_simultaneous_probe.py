"""Observe simultaneous recorder ownership with actual public run lifecycles.

Architecture: independent SDK -> two live runs and framework recorders -> exact
completion/errors -> actual journals and immutable histories -> repeat teardown.
Main caller: test_framework_init_gym_simultaneous_contracts. Boundary mode replaces
only unavailable recorder/encoding dependencies; real mode uses installed Gym,
NumPy, MoviePy and MP4 files. Pinned ownership and old-library bugs remain raw.
"""
from contextlib import ExitStack
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import warnings

from framework_init_runtime import histories
from gym_fixtures import library

REFERENCE = sys.argv[1] == 'reference'
REAL = sys.argv[2] == 'real'
OUTPUT = Path(sys.argv[-1])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk

SETTINGS = dict(console='off', silent=True, disable_git=True, disable_code=True,
                x_disable_stats=True, init_timeout=5)
LIBRARY = os.environ.get('BACKPROP_GYM_LIBRARY', 'gym') if REAL else 'gym'


def outcome(call):
    try:
        value = call()
        return {'value': None if value is None else value}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error)}


def environment(gym):
    """Build a real rendered environment without an external simulator.

    Main caller: recording. The framework owns step/reset and recorder lifetimes;
    this environment provides deterministic rewards and RGB frames for real
    encoders, never a replacement recorder or prebuilt media file.
    """
    import numpy as np
    class Environment(gym.Env):
        metadata = {'render_modes': ['rgb_array'], 'render_fps': 4}
        render_mode = 'rgb_array'
        observation_space = gym.spaces.Box(0, 1, shape=(1,), dtype=np.float32)
        action_space = gym.spaces.Discrete(2)
        def __init__(self):
            self.frame = 0
        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            self.frame = 0
            return np.zeros(1, dtype=np.float32), {}
        def step(self, action):
            self.frame += 1
            return np.zeros(1, dtype=np.float32), 1.0, self.frame >= 3, False, {}
        def render(self):
            return np.full((24, 32, 3), self.frame * 70, dtype=np.uint8)
    return Environment()


def recording(gym, directory, name):
    """Start one recording while the intended automatic global owner is live.

    Main caller: case. Boundary mode uses the existing completion-hook fixture;
    genuine mode delegates recording schedules and encoding to the installed
    wrapper/recorder, preserving version-specific upstream failures.
    """
    if not REAL:
        recorder = importlib.import_module('gym.wrappers.monitoring.video_recorder').VideoRecorder(
            Path(directory) / (name + '.mp4'))
        return {'recorder': recorder, 'folder': Path(directory), 'name': name, 'reward': 1.0}
    env = environment(gym)
    env.reset()
    modern = LIBRARY == 'gymnasium' and hasattr(gym.wrappers.RecordVideo, 'stop_recording')
    folder = Path(directory) / name
    folder.mkdir()
    if modern:
        recorder = gym.wrappers.RecordVideo(env, str(folder), episode_trigger=lambda _: True,
                                           name_prefix=name, disable_logger=True)
        recorder.reset()
        reward = recorder.step(0)[1]
    else:
        recorder_type = importlib.import_module(LIBRARY + '.wrappers.monitoring.video_recorder').VideoRecorder
        recorder = recorder_type(env, path=str(folder / (name + '.mp4')))
        recorder.capture_frame()
        reward = env.step(0)[1]
        recorder.capture_frame()
    return {'recorder': recorder, 'folder': folder, 'name': name, 'reward': reward, 'env': env}


def media(record):
    return [{'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'bytes': path.stat().st_size, 'mp4_header': b'ftyp' in path.read_bytes()[:32]}
            for path in sorted(record['folder'].glob(record['name'] + '*.mp4'))]


def case(directory, scenario):
    """Record under reinit, then recover a fresh run after ordinary teardown.

    Main caller: observe. Explicit handle metrics remain per-run even when
    automatic video discovery belongs to the module's global handle. Closing an
    old recording after finish_previous must never attach it to its replacement.
    """
    sdk.teardown()
    gym = importlib.import_module(LIBRARY)
    options = dict(mode='offline', project='gym-simultaneous', entity='e', dir=directory, settings=SETTINGS)
    first_policy = 'create_new' if scenario == 'only_create_new' else 'finish_previous'
    first = sdk.init(id='first-' + scenario, monitor_gym=True, reinit=first_policy, **options)
    one = recording(gym, directory, scenario + '-first')
    boundary_selected = None
    if not REAL:
        if REFERENCE:
            boundary_selected = any(item[0] == 'gym.wrappers.monitoring.video_recorder.VideoRecorder'
                                    for item in sdk.patched['gym'])
        else:
            from backprop.integration.gym import _patches
            boundary_selected = type(one['recorder']) in _patches
    first.log({'first_reward': one['reward']})
    second_policy = 'finish_previous' if scenario == 'finish_previous' else 'create_new'
    second = sdk.init(id='second-' + scenario, monitor_gym=True, reinit=second_policy, **options)
    two = recording(gym, directory, scenario + '-second')
    second.log({'second_reward': two['reward']})
    global_id = getattr(sdk.run, 'id', None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        first_close = outcome(one['recorder'].close)
        second_close = outcome(two['recorder'].close)
    first.finish()
    second.finish()
    first_rows = histories(first, REFERENCE, reference_output=OUTPUT.with_name(OUTPUT.stem + '-' + scenario + '-first.wandb') if REFERENCE else None)
    second_rows = histories(second, REFERENCE, reference_output=OUTPUT.with_name(OUTPUT.stem + '-' + scenario + '-second.wandb') if REFERENCE else None)
    result = {'global_id': global_id, 'first_close': first_close, 'second_close': second_close,
              'boundary_recorder_selected': boundary_selected,
              'first_videos': len([row for row in first_rows if 'videos' in row]),
              'second_videos': len([row for row in second_rows if 'videos' in row]),
              'first_rewards': [row['first_reward'] for row in first_rows if 'first_reward' in row],
              'second_rewards': [row['second_reward'] for row in second_rows if 'second_reward' in row],
              'media': media(one) + media(two),
              'warnings': [str(item.message) for item in caught], 'global_empty': sdk.run is None}
    for record in (one, two):
        if REAL:
            record['env'].close()
    sdk.teardown()
    repeated = sdk.init(id='repeat-' + scenario, monitor_gym=True, **options)
    again = recording(gym, directory, scenario + '-repeat')
    result['repeat_close'] = outcome(again['recorder'].close)
    repeated.finish()
    rows = histories(repeated, REFERENCE,
        reference_output=OUTPUT.with_name(OUTPUT.stem + '-' + scenario + '-repeat.wandb') if REFERENCE else None)
    result['repeat_videos'] = len([row for row in rows if 'videos' in row])
    result['repeat_media'] = media(again)
    if REAL:
        again['env'].close()
    sdk.teardown()
    result['repeat_global_empty'] = sdk.run is None
    if REFERENCE:
        result['hooks_after_teardown'] = len(sdk.patched['gym'])
    else:
        from backprop.integration.gym import _patches
        result['hooks_after_teardown'] = len(_patches)
    return result


with tempfile.TemporaryDirectory(prefix='framework-init-gym-simultaneous-') as directory, ExitStack() as stack:
    os.environ['NETRC'] = str(Path(directory) / 'netrc')
    os.environ['WANDB_CONFIG_DIR'] = str(Path(directory) / 'settings')
    if not REAL:
        # Both SDKs prefer installed Gymnasium. Boundary mode deliberately
        # exercises the controlled Gym recorder, independent of CI extras;
        # genuine mode above keeps every installed framework untouched.
        stack.enter_context(patch.dict(sys.modules, {**library('gym', '0.26.2'), 'gymnasium': None}))
        stack.enter_context(patch.object(sdk, 'Video', side_effect=lambda path: Path(path).name))
    gym = importlib.import_module(LIBRARY)
    root = Path(sdk.__file__).parent
    source_names = ['sdk/wandb_init.py', 'integration/gym/__init__.py'] if REFERENCE else [
        '__init__.py', 'init_integrations.py', 'integration/gym/__init__.py']
    observations = {name: case(directory, name) for name in ['global_create_new', 'only_create_new', 'finish_previous']}
    OUTPUT.write_text(json.dumps({'sdk': sdk.__version__, 'python': sys.version, 'mode': 'real' if REAL else 'boundary',
        'library': LIBRARY, 'version': gym.__version__,
        'runtime': {name: importlib.metadata.version(name) for name in ['numpy', 'moviepy', 'imageio_ffmpeg']} if REAL else {},
        'source_hashes': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_names},
        'observations': observations}, indent=2, sort_keys=True) + '\n')
