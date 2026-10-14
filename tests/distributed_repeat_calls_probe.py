"""Compare repeated public artifact calls with pinned preparation and encoding.

Architecture: real Run/Artifact methods -> changed call arguments -> hosted
publication boundary (reference) or durable envelopes (native). This checks
observable intent and validation; distributed closure/retry tests separately
verify immutable generations and failure recovery. No cloud service is used.
"""
import copy,hashlib,importlib,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import patch
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    from wandb.sdk.interface.interface import InterfaceBase
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    from backprop.transport import read_journal
    install_wandb_aliases()


def build():
    out={}
    for module in ['wandb','wandb.sdk.wandb_run']:
      for operation in ['upsert_artifact','finish_artifact']:
       for mode in ['online','offline']:
        for case in ['same','aliases','metadata','description','other_group','invalid_group','invalid_alias','object_options','other_run']:
         with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory,'WANDB_DATA_DIR':directory+'/data','WANDB_CACHE_DIR':directory+'/cache'}):
            published=[];runs=[]
            def make_run(name):
                settings=dict(console='off',silent=True,disable_git=True,x_disable_stats=True)
                if REFERENCE:
                    run=w.Run(settings=w.Settings(mode=mode,run_id=name,entity='e',project='p',run_group='parts',**settings))
                    boundary=S(_make_artifact_manifest=lambda *a,**k:None)
                    def publish(owner,artifact,aliases,*args,**kwargs):
                        group=InterfaceBase._make_artifact(boundary,artifact).distributed_id
                        # Pinned _resolve_aliases creates a set, so publication
                        # order varies by Python hash seed. Compare membership.
                        published.append({'group':group,'aliases':sorted(aliases),'metadata':copy.deepcopy(artifact.metadata),'description':artifact.description})
                        return object()
                    run._cached_public_api=S(settings={'entity':'e','project':'p'},_expected_artifact_type=lambda **kw:None,_set_artifact_save_handle=lambda *a:None)
                    run._interface=S(publish_artifact=publish,deliver_artifact=publish,deliver_history_step=lambda:S(wait_or=lambda **kw:S(step=0)))
                else:
                    run=w.init(id=name,entity='e',project='p',group='parts',mode=mode,dir=directory,reinit='create_new',
                        _sink=w.MemorySink() if mode=='online' else None,_deployment={},settings={**settings,'flush_interval':600})
                runs.append(run);return run
            run=make_run('writer');artifact=w.Artifact('model','model',metadata={'round':1},description='original')
            method=getattr(importlib.import_module(module).Run,operation)
            method(run,artifact,aliases=['initial'])
            options={'aliases':['initial']}
            if case=='aliases':options['aliases']=['changed']
            if case=='metadata':artifact.metadata={'round':2}
            if case=='description':artifact.description='changed'
            if case=='other_group':options['distributed_id']='other'
            if case=='invalid_group':options['distributed_id']=True
            if case=='invalid_alias':options['aliases']=['bad/name']
            if case=='object_options':options.update(name='ignored/name',type='ignored')
            if case=='other_run':run=make_run('other')
            try:
                returned=method(run,artifact,**options)
                if REFERENCE:manifest=published[-1]
                else:
                    event=next(event for event in reversed(read_journal(run.sync_dir)) if event['type']==('artifact_part' if operation=='upsert_artifact' else 'artifact'))
                    payload=event['payload']['manifest'] if operation=='upsert_artifact' else event['payload']
                    manifest={key:payload.get(key) for key in ['aliases','metadata','description']}
                    manifest['aliases']=sorted(manifest['aliases'])
                    manifest['group']=event['payload']['distributed_id']
                result={'same_object':returned is artifact,'manifest':manifest,'local_group':repr(artifact.distributed_id)}
            except Exception as error:
                result={'error':type(error).__name__,'message':str(error),'local_group':repr(artifact.distributed_id)}
            out[f'{module}/{operation}/{mode}/{case}']=result
            for run in runs:
                if REFERENCE:run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
                else:run.finish()
    if REFERENCE:
        out['source_hashes']={}
        root=Path(__file__).resolve().parents[1]
        for module in ['wandb.sdk.wandb_run','wandb.sdk.interface.interface','wandb.util']:
            path=Path(importlib.import_module(module).__file__)
            assert path.read_bytes()==(root/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')).read_bytes()
            out['source_hashes'][module]=hashlib.sha256(path.read_bytes()).hexdigest()
    return out


if __name__=='__main__':print(json.dumps(build(),sort_keys=True))
