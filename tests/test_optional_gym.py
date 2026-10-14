"""Exercise actual Gym/Gymnasium encoders in isolated version-matrix CI.

Architecture: tiny rendered environment -> framework-owned recorder/wrapper ->
Backprop video journal -> public history and verified original MP4 bytes.
Main caller: optional-gym CI job; no packages are installed by this test.
"""
import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

import backprop as w
from backprop.apis.public import Api
from backprop.integration import gym as adapter

LIBRARY=os.environ.get("BACKPROP_GYM_LIBRARY") or ("gymnasium" if importlib.util.find_spec("gymnasium") else "gym")
AVAILABLE=all(importlib.util.find_spec(name) is not None for name in (LIBRARY,"numpy","moviepy","imageio_ffmpeg"))


@unittest.skipUnless(AVAILABLE,"Optional Gym/Gymnasium and video encoder dependencies are not installed")
class OptionalGym(unittest.TestCase):
    def setUp(self):
        self.gym=importlib.import_module(LIBRARY)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=w.MemorySink()
        self.run=w.init(entity="e",project="gym-runtime",dir=self.tmp.name,_sink=self.store,monitor_gym=True,
                        settings={"console":"off","silent":True,"x_disable_stats":True},reinit=True)
        self.addCleanup(w.teardown)
        self.api=Api._from_store(self.store,entity="e",project="gym-runtime")

    def environment(self):
        import numpy as np
        gym=self.gym
        class Environment(gym.Env):
            metadata={"render_modes":["rgb_array"],"render_fps":4}
            render_mode="rgb_array"
            observation_space=gym.spaces.Box(0,1,shape=(1,),dtype=np.float32)
            action_space=gym.spaces.Discrete(2)
            def __init__(self):self.frame=0
            def reset(self,*,seed=None,options=None):
                super().reset(seed=seed);self.frame=0
                return np.zeros(1,dtype=np.float32),{}
            def step(self,action):
                self.frame+=1
                return np.zeros(1,dtype=np.float32),1.0,self.frame>=2,False,{}
            def render(self):return np.full((24,32,3),self.frame*70,dtype=np.uint8)
        return Environment()

    def assert_videos(self, count):
        self.run.flush();rows=list(self.api.run(self.run.id).scan_history())
        values=[row["videos"] for row in rows if "videos" in row]
        self.assertEqual(len(values),count)
        for value in values:
            data=self.store.download_file(value)
            self.assertIn(b"ftyp",data[:32]);self.assertGreater(len(data),100)
        return values

    def test_real_recording_and_alias_reuse(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        for name in ("wandb.gym","wandb.integration.gym"):
            module=importlib.import_module(name)
            self.assertIs(module,adapter);self.assertIsNone(module.monitor())
        environment=self.environment();environment.reset()
        if LIBRARY=="gymnasium" and hasattr(self.gym.wrappers.RecordVideo,"stop_recording"):
            folder=Path(self.tmp.name)/"video"
            recorder=self.gym.wrappers.RecordVideo(environment,str(folder),episode_trigger=lambda _:True,disable_logger=True)
            recorder.reset();recorder.step(0);recorder.step(0);recorder.close();recorder.close()
            path=next(folder.glob("*.mp4"))
        else:
            recorder_module=importlib.import_module(LIBRARY+".wrappers.monitoring.video_recorder")
            path=Path(self.tmp.name)/"direct.mp4"
            recorder=recorder_module.VideoRecorder(environment,path=str(path))
            recorder.capture_frame();environment.step(0);recorder.capture_frame()
            recorder.close();recorder.close()
        values=self.assert_videos(1)
        self.assertEqual(self.store.download_file(values[0]),path.read_bytes())

    def test_wrapper_episode_schedule_records_every_completed_episode(self):
        if LIBRARY=="gym":self.skipTest("Legacy Gym direct recorder is exercised separately")
        folder=Path(self.tmp.name)/"episodes"
        env=self.gym.wrappers.RecordVideo(self.environment(),str(folder),episode_trigger=lambda _:True,disable_logger=True)
        try:
            for episode in range(3):
                env.reset();env.step(0);env.step(0)
        finally:env.close()
        self.assertEqual(len(list(folder.glob("*.mp4"))),3)
        self.assert_videos(3)

    def test_empty_recording_does_not_try_to_upload_a_missing_file(self):
        environment=self.environment();environment.reset()
        if LIBRARY=="gymnasium" and hasattr(self.gym.wrappers.RecordVideo,"stop_recording"):
            recorder=self.gym.wrappers.RecordVideo(environment,str(Path(self.tmp.name)/"empty"),disable_logger=True)
            recorder.start_recording("empty");recorder.stop_recording();recorder.close()
        else:
            module=importlib.import_module(LIBRARY+".wrappers.monitoring.video_recorder")
            recorder=module.VideoRecorder(environment,path=str(Path(self.tmp.name)/"empty.mp4"))
            recorder.close()
        self.assert_videos(0)
