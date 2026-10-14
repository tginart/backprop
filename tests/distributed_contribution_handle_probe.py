"""Reproduce the contribution-only handle lifecycle against pinned W&B.

Architecture: real public Run.upsert_artifact -> service response fixture or
native MemorySink -> actual Artifact.wait/state/id observations before and after
primary finalization. Only the upstream hosted response boundary is substituted;
the reference wait and property implementations execute unchanged. No W&B request
is sent. Main caller: isolated reference/native audit subprocesses.

Why this exists: accepted contributions are not committed artifacts. The returned
handle must distinguish acceptance from a primary's later combined publication.
Pass reference or native as the sole argument, with the corresponding PYTHONPATH.
"""
import hashlib,importlib,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:import wandb as w
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
module=sys.argv[2] if len(sys.argv)>2 else 'wandb'
Artifact=importlib.import_module(module).Artifact
with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory,'WANDB_DATA_DIR':directory+'/data','WANDB_CACHE_DIR':directory+'/cache'}):
 settings=dict(console='off',silent=True,disable_git=True,x_disable_stats=True)
 if reference:
  attrs=S(id='opaque-server-id',artifact_sequence=S(name='model',project=S(name='p',entity=S(name='e'))),version_index=0,
   artifact_type=S(name='model'),description=None,aliases=[],tags=[],metadata={},ttl_duration_seconds=-2,ttl_is_inherited=True,
   state='PENDING',size=1,digest='manifest-digest',digest_algorithm=None,commit_hash=None,file_count=1,
   created_at='2026-10-06T00:00:00Z',updated_at='2026-10-06T00:00:00Z',history_step=0)
  service=S(feature_enabled=lambda feature:True,execute_graphql=lambda *a,**kw:S(artifact=attrs))
  save_handle=S(wait_or=lambda **kw:S(response=S(log_artifact_response=S(artifact_id=attrs.id,error_message=''))))
  run=w.Run(settings=w.Settings(mode='online',run_id='worker',entity='e',project='p',run_group='parts',**settings))
  run._cached_public_api=S(settings={'entity':'e','project':'p'},_expected_artifact_type=lambda **kw:None,
   _set_artifact_save_handle=lambda artifact,handle:artifact._set_save_handle(handle,service))
  run._interface=S(deliver_artifact=lambda *a,**kw:save_handle,deliver_history_step=lambda:S(wait_or=lambda **kw:S(step=0)))
 else:
  sink=w.MemorySink();run=w.init(id='worker',entity='e',project='p',group='parts',mode='online',dir=directory,
   _sink=sink,_deployment={},settings={**settings,'flush_interval':600},reinit='create_new')
 artifact=Artifact('model','model')
 with artifact.new_file('worker') as out:out.write('worker')
 run.upsert_artifact(artifact)
 def observe():
  try:value={'wait_returns_self':Artifact.wait(artifact) is artifact}
  except Exception as error:value={'error':type(error).__name__,'message':str(error)}
  value.update(state=artifact.state,is_draft=artifact.is_draft(),has_public_id=artifact.id is not None)
  return value
 result={'after_contribution':observe()}
 if reference:attrs.state='COMMITTED'
 else:
  with w.init(id='primary',entity='e',project='p',group='parts',mode='online',dir=directory,_sink=sink,_deployment={},settings={**settings,'flush_interval':600},reinit='create_new') as primary:
   final=w.Artifact('model','model')
   with final.new_file('primary') as out:out.write('primary')
   primary.finish_artifact(final)
 result['after_primary_commit']=observe()
 if reference:run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
 else:run.finish()
 if reference:
  actual=Path(importlib.import_module('wandb.sdk.artifacts.artifact').__file__)
  expected=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb/sdk/artifacts/artifact.py'
  assert actual.read_bytes()==expected.read_bytes()
  result['source_sha256']=hashlib.sha256(actual.read_bytes()).hexdigest()
 print(json.dumps(result))
