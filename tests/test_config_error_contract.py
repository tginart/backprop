"""Verify the config error exposed by repeated framework initialization.

Architecture: isolated pinned/native processes -> identical existing config edits
-> error class/text and unchanged values. Main caller: SDK release verification.
Why this exists: XGBoost can encounter changed tree counts when before_training
is called twice; comparison must inspect the real public error, not mask it.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PROBE='''
import json,sys
if sys.argv[1]=='native':
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
import wandb
from wandb.sdk.wandb_config import Config
out=[]
for before,after in [(1,2),({'learner':{'num_trees':0}},{'learner':{'num_trees':3}})]:
 config=Config();config.update({'value':before})
 try:config.update({'--value--':after},allow_val_change=False)
 except Exception as error:out.append({'error':type(error).__name__,'message':str(error),'unchanged':config['value']==before})
 else:raise AssertionError('Changed configuration must raise')
 config.update({'value':after},allow_val_change=True)
 assert config['value']==after
print(json.dumps(out))
'''

class ConfigErrorContract(unittest.TestCase):
    def test_changed_value_diagnostics_match_pinned_and_preserve_state(self):
        from importlib.metadata import version,PackageNotFoundError
        try:reference_version=version('wandb')
        except PackageNotFoundError:self.skipTest('Pinned W&B reference wheel is unavailable')
        self.assertEqual(reference_version,'0.30.0')
        values=[]
        with tempfile.TemporaryDirectory() as directory:
            for mode in ['reference','native']:
                result=subprocess.run([sys.executable,'-B','-c',PROBE,mode],cwd=directory,
                    env={**os.environ,'PYTHONPATH':str(ROOT/'sdk') if mode=='native' else ''},
                    capture_output=True,text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stderr)
                values.append(json.loads(result.stdout))
        self.assertEqual(values[0],values[1]);self.assertTrue(all(v['unchanged'] for v in values[1]))
