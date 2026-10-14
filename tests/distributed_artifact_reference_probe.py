"""Compare distributed public calls with pinned W&B 0.30.0.

Architecture: genuine Run/Artifact/Settings -> public call -> explicit hosted
publication/type lookup fixture. Native calls use durable journals. Main caller:
test_distributed_artifact_contract. Merge and recovery have separate tests.
"""
import importlib,inspect,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:import wandb as w
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
result={}
for label in ['wandb','wandb.sdk.wandb_run']:
 cls=importlib.import_module(label).Run
 for operation in ['upsert_artifact','finish_artifact']:
  for mode in ['online','offline','disabled','finished']:
   for kind in ['ordinary','committed','empty_id','group_default','empty_group','missing_group','file','bad_alias','queued','finalized','incremental','directory','pathlib','missing','none','missing_arg','extra_args','unknown_arg','default_type','custom_name','object_options']:
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory,'WANDB_CACHE_DIR':directory+'/cache'}):
     settings=dict(silent=True,console='off',disable_git=True,x_disable_stats=True)
     group='workers' if kind=='group_default' else '' if kind=='empty_group' else None
     distributed_id=None if kind in ['group_default','empty_group','missing_group'] else '' if kind=='empty_id' else 'parts'
     if reference:
      run=w.init(mode='disabled',settings=settings) if mode=='disabled' else cls(settings=w.Settings(mode='offline' if mode=='offline' else 'online',run_id='r',entity='e',project='p',run_group=group,**settings))
      run._cached_public_api=SimpleNamespace(settings={'entity':'e','project':'p'},_expected_artifact_type=lambda **kwargs:None,_set_artifact_save_handle=lambda *args:None)
      run._interface=SimpleNamespace(deliver_artifact=lambda *args,**kwargs:object(),publish_artifact=lambda *args,**kwargs:None,deliver_history_step=lambda:SimpleNamespace(wait_or=lambda **kwargs:SimpleNamespace(step=0)))
     else:run=w.init(id='r',entity='e',project='p',group=group,mode=mode if mode!='finished' else 'online',dir=directory,_sink=w.MemorySink() if mode in ['online','finished'] else None,_deployment={},settings={**settings,'flush_interval':600})
     path=Path(directory)/'weights';path.write_text('bytes')
     artifact=w.Artifact('model','model',incremental=kind=='incremental');artifact.add_file(str(path))
     if kind=='finalized':artifact.finalize()
     if kind=='queued':run.log_artifact(artifact)
     if kind=='committed' and mode!='disabled':
      if reference:
       from wandb.sdk.artifacts.artifact import ArtifactState
       artifact._state=ArtifactState.COMMITTED;artifact._id='committed-source'
       artifact._source_entity=artifact._entity='e';artifact._source_project=artifact._project='p';artifact._version='v0'
      else:
       run.log_artifact(artifact)
       if mode=='offline':w.sync(run.sync_dir,sink=w.MemorySink())
       else:run.flush()
      committed_id=artifact.id
     tree=Path(directory)/'tree';tree.mkdir();(tree/'weights').write_text('bytes')
     value=(str(path) if kind in ['file','default_type','custom_name'] else str(tree) if kind=='directory' else path if kind=='pathlib' else str(Path(directory)/'absent') if kind=='missing' else None if kind=='none' else artifact)
     args=[] if kind=='missing_arg' else [value,None,None,None,None,None] if kind=='extra_args' else [value]
     kwargs={} if kind in ['missing_arg','extra_args'] else {'type':'model','distributed_id':distributed_id,'aliases':['bad/alias'] if kind=='bad_alias' else None}
     if kind=='default_type':kwargs.pop('type')
     if kind=='custom_name':kwargs['name']='custom'
     if kind=='object_options':kwargs.update(name='ignored/name',type='ignored')
     if kind=='unknown_arg':kwargs['unknown']=True
     if mode=='finished':
      if reference:run._is_finished=True
      else:run.finish()
     try:
      got=getattr(run,operation)(*args,**kwargs)
      outcome={'return':'Noop'} if mode=='disabled' else {'return':'Artifact','same_object':got is value if isinstance(value,w.Artifact) else None,'distributed_id':got.distributed_id,'files':sorted(got.manifest.entries)}
      if kind=='committed' and mode!='disabled':outcome['committed_identity_preserved']=got.id==committed_id
     except Exception as error:outcome={'error':type(error).__name__,'message':str(error).replace(directory,'<tmp>')}
     result[f'{label}.Run.{operation}/{mode}/{kind}']=outcome
     if reference:
      run._is_finished=True
      importlib.import_module('wandb.sdk.lib.module').unset_globals()
     else:run.finish()
result['signatures']={f'{module}.Run.{method}':[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(getattr(importlib.import_module(module).Run,method)).parameters.values()] for module in ['wandb','wandb.sdk.wandb_run'] for method in ['upsert_artifact','finish_artifact']}
if reference:
 import hashlib
 source=Path(importlib.import_module('wandb.sdk.wandb_run').__file__)
 expected=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb/sdk/wandb_run.py'
 assert source.read_bytes()==expected.read_bytes()
 result['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
print(json.dumps(result))
