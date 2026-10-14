"""Compare wait lifecycle guards through actual public Artifact imports.

Architecture: real constructor and offline publication -> public wait with valid
and invalid arguments -> error identity/message or object return. The committed
fixture sets only server-owned lifecycle state. Main caller: wait contract tests.
No hosted service or background W&B process is required.
"""
import importlib,inspect,json,os,sys,tempfile
from types import SimpleNamespace
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:
 import wandb as w
 from wandb.sdk.artifacts.artifact import ArtifactState
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
results={}
for module in ['wandb','wandb.sdk','wandb.sdk.artifacts.artifact']:
 cls=importlib.import_module(module).Artifact
 for stage in ['draft','finalized','offline_log','offline_upsert','committed']:
  for name,value in [('default',None),('zero',0),('negative',-1),('positive',1),('string','bad'),('unknown',None),('extra',None)]:
   with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
    artifact=cls('model','model');run=None
    if stage=='finalized':artifact.finalize()
    if stage.startswith('offline'):
     if reference:
      run=w.Run(settings=w.Settings(mode='offline',run_id='r',entity='e',project='p',run_group='group',console='off',silent=True))
      run._interface=SimpleNamespace(publish_artifact=lambda *args,**kwargs:None)
     else:
      run=w.init(id='r',entity='e',project='p',group='group',mode='offline',dir=directory,_deployment={},settings={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True})
     (run.log_artifact if stage=='offline_log' else run.upsert_artifact)(artifact)
    if stage=='committed':
     if reference:artifact._state=ArtifactState.COMMITTED
     else:artifact._logged=artifact._committed=True
    args=[None,1] if name=='extra' else [];kwargs={'unknown':1} if name=='unknown' else {} if name=='extra' else {'timeout':value}
    try:result={'returns_self':artifact.wait(*args,**kwargs) is artifact}
    except Exception as error:result={'error':type(error).__name__,'message':str(error)}
    results[f'{module}/{stage}/{name}']=result
    if run:
     if reference:
      run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
     else:run.finish()
if reference:
 import hashlib
 from pathlib import Path
 path=Path(importlib.import_module('wandb.sdk.artifacts.artifact').__file__)
 expected=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb/sdk/artifacts/artifact.py'
 assert path.read_bytes()==expected.read_bytes()
 results['source_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(results))
