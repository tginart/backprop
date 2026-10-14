"""Observe real pinned publication and serialization at its hosted boundary.

Architecture: genuine Run/Artifact -> public log_artifact -> real InterfaceBase
serialization -> captured wire record. Only delivery and the hosted type query
are fixtures. Native observations use actual journals. Main caller: namespace
contract tests. A committed reference object uses explicit server-result fields;
this fixture does not infer server-side cross-project deduplication behavior.
"""
import hashlib,importlib,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace,MethodType
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    from wandb.sdk.interface.interface import InterfaceBase
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def build():
    observations={}
    for entry in ['wandb','wandb.Run','wandb.sdk.wandb_run.Run']:
      cls=w.Run if entry=='wandb' else getattr(importlib.import_module(entry.rsplit('.',1)[0]),'Run')
      for committed in [False,True]:
       for mode in ['online','offline']:
        for entity,project in [('e','p'),('e','q'),('other','p')]:
         key=f'{entry}/{committed}/{mode}/{entity}/{project}'
         with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'BACKPROP_DIR':temp,'WANDB_CACHE_DIR':temp+'/cache'}),patch.object(w,'termwarn'):
          settings=dict(silent=True,console='off',disable_git=True,x_disable_stats=True)
          artifact=w.Artifact('model','model')
          with artifact.new_file('weights.bin') as out:out.write('weights')
          records=[]
          if REFERENCE:
            artifact.finalize()
            if committed:
              artifact._id='server-id';artifact._state='COMMITTED'
              artifact._entity=artifact._source_entity='e'
              artifact._project=artifact._source_project='p'
              artifact._name=artifact._source_name='model:v0';artifact._version='v0'
            run=cls(settings=w.Settings(mode=mode,run_id='copy',entity=entity,project=project,**settings))
            importlib.import_module('wandb.sdk.wandb_init')._set_global_run(run)
            run._cached_public_api=SimpleNamespace(settings={'entity':entity,'project':project},_expected_artifact_type=lambda **kwargs:None,_set_artifact_save_handle=lambda *args:None)
            transport=SimpleNamespace(_deliver_artifact=lambda request:records.append(request.artifact),_publish_artifact=records.append, deliver_history_step=lambda:SimpleNamespace(wait_or=lambda **kwargs:SimpleNamespace(step=0)))
            for method in ['_make_run','_make_config','_make_artifact','_make_artifact_manifest','deliver_artifact','publish_artifact']:
              setattr(transport,method,MethodType(getattr(InterfaceBase,method),transport))
            run._interface=transport
          else:
            sink=w.MemorySink()
            with w.init(id='source',entity='e',project='p',mode='online' if committed else 'offline',dir=temp,_sink=sink if committed else None,_deployment={},settings={**settings,'flush_interval':600}) as source:
              source.log_artifact(artifact)
            run=w.init(id='copy',entity=entity,project=project,mode=mode,dir=temp,_sink=sink if mode=='online' else None,_deployment={},settings={**settings,'flush_interval':600})
          try:
            result=getattr(w if entry=='wandb' else run,'log_artifact')(artifact,aliases=['release'],tags=['trained'])
            if REFERENCE:
              record=records[-1]
              obs={'same_object':result is artifact,'scope':[record.entity,record.project,record.run_id],
                'collection':record.name.split(':')[0],'files':sorted(e.path for e in record.manifest.contents),
                'aliases':sorted(record.aliases),'tags':list(record.tags)}
            else:
              from backprop.transport import read_journal
              events=read_journal(run.sync_dir);record=next(e for e in events if e['type']=='artifact')
              update=next(e for e in events if e['type']=='artifact_update')
              obs={'same_object':result is artifact,'scope':[record['entity'],record['project'],record['run_id']],
                'collection':record['payload']['name'],'files':sorted(e['name'] for e in record['payload']['files']),
                'aliases':sorted(update['payload']['alias_add']),'tags':update['payload']['values']['tags']}
          except Exception as error:
            if not isinstance(error,ValueError):raise
            obs={'error':type(error).__name__}
          observations[key]=obs
          if REFERENCE:
            run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
          else:run.finish()
    if REFERENCE:
      observations['source_hashes']={}
      for module in ['wandb.sdk.interface.interface','wandb.sdk.wandb_run','wandb.sdk.artifacts.artifact']:
        path=Path(importlib.import_module(module).__file__)
        assert path.read_bytes()==(ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')).read_bytes()
        observations['source_hashes'][module]=hashlib.sha256(path.read_bytes()).hexdigest()
    return observations

if __name__=='__main__':print(json.dumps(build()))
