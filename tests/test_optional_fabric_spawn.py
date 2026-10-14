"""Verify native logger restoration through real Fabric process launch.

Architecture: separate CPU DDP launch processes -> real logger serialization ->
public run identity and journal replay checks. Main caller: lower/current
Lightning CI. Missing local frameworks remain explicit verification skips.
"""
import importlib.util
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.transport import MemorySink

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('lightning'), 'Optional Lightning Fabric required')
class FabricSpawnContracts(unittest.TestCase):
    def test_actual_two_rank_fabric_launch_online_and_offline_repeated_sessions(self):
        for mode in ['online','offline']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/fabric_spawn_probe.py'),directory,mode],
                    capture_output=True,text=True,timeout=150,
                    env={**os.environ,'PYTHONPATH':str(ROOT/'sdk'),'OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'})
                self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
                observed=json.loads(result.stdout.splitlines()[-1])
                self.assertEqual(observed['writers'],3);self.assertEqual(observed['history_rows'],7)
                self.assertEqual(observed['mode'],mode);self.assertEqual(observed['state'],'finished')
                self.assertEqual(len({row['pid'] for row in observed['rounds']}),4)
                if os.environ.get('CI'):
                    output=ROOT/'test-results';output.mkdir(exist_ok=True)
                    (output/f'fabric-spawn-{mode}.json').write_text(json.dumps(observed,indent=2)+'\n')

    def test_supplied_run_pickle_preserves_identity_and_unrelated_global(self):
        from backprop.integration.lightning.fabric import WandbLogger
        from lightning.fabric.utilities.rank_zero import rank_zero_only
        backprop.teardown()
        with tempfile.TemporaryDirectory() as directory,patch.object(rank_zero_only,'rank',0):
            settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}
            parent=backprop.init(id='provided',entity='e',project='p',dir=directory,_sink=MemorySink(),settings=settings)
            try:
                logger=WandbLogger(experiment=parent,version='ignored',save_dir=directory,log_model='all')
                restored=pickle.loads(pickle.dumps(logger))
                self.assertEqual(restored.version,parent.id);self.assertIs(restored.experiment,parent)
                parent.flush()
                other=backprop.init(id='other',entity='e',project='p',dir=directory,_sink=MemorySink(),settings=settings,reinit='create_new')
                with patch.object(backprop,'run',other):
                    fresh=pickle.loads(pickle.dumps(logger))
                    child=fresh.experiment
                    self.assertEqual(child.path,parent.path);self.assertIsNot(child,parent)
                    self.assertIs(backprop.run,other);self.assertFalse(child.settings.x_primary)
                    child.finish();self.assertFalse(parent._closed);self.assertFalse(other._closed)
            finally:backprop.teardown()


if __name__=='__main__':unittest.main()
