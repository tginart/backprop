"""Compare artifact descriptors through each supported public import.

Architecture: genuine pinned/native constructors -> local descriptor operations
in draft/finalized/committed state -> values, object identity, errors, signatures.
The committed fixture sets only persisted lifecycle state; no descriptor or
constructor code is replaced. Main caller: distributed property contract tests.
"""
import hashlib,importlib,inspect,json,sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    from wandb.sdk.artifacts.artifact import ArtifactState
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def values():
    return {'none':None,'empty':'','text':'workers','unicode':'café','integer':42,
        'zero':0,'false':False,'true':True,'list':[],'dictionary':{},'bytes':b'group',
        'path':Path('group'),'object':object()}


def observe(value,original):
    encoded=value.hex() if isinstance(value,bytes) else str(value) if isinstance(value,Path) else None if type(value) is object else value
    return {'type':type(value).__name__,'same_object':value is original,'value':encoded}


def outcome(call):
    try:return {'return':call()}
    except Exception as error:return {'error':type(error).__name__,'message':str(error)}


def stage(artifact,name):
    if name!='draft':artifact.finalize()
    if name=='committed':
        if REFERENCE:artifact._state=ArtifactState.COMMITTED
        else:artifact._logged=artifact._committed=True


def build():
    result={}
    for module in ['wandb','wandb.sdk','wandb.sdk.artifacts.artifact']:
        cls=importlib.import_module(module).Artifact;group={}
        for state in ['draft','finalized','committed']:
            for name,value in values().items():
                artifact=cls('model','model');stage(artifact,state)
                def change():
                    artifact.distributed_id=value
                    return observe(artifact.distributed_id,value)
                group[f'{state}/distributed_id/{name}']=outcome(change)
                if isinstance(value,list):
                    value.append('changed');assert artifact.distributed_id==['changed']
                elif isinstance(value,dict):
                    value['changed']=True;assert artifact.distributed_id=={'changed':True}
                group[f'{state}/distributed_id/{name}/delete']=outcome(lambda:delattr(artifact,'distributed_id'))
            for name,value in {'true':True,'false':False,'zero':0,'one':1,'none':None,'text':'yes','list':[],'dictionary':{}}.items():
                with patch.object(w,'termwarn'):
                    artifact=cls('model','model',incremental=value)
                stage(artifact,state)
                group[f'{state}/incremental/{name}']=observe(artifact.incremental,value)
                group[f'{state}/incremental/{name}/set']=outcome(lambda:setattr(artifact,'incremental',False))
                group[f'{state}/incremental/{name}/delete']=outcome(lambda:delattr(artifact,'incremental'))
        group['signatures']={}
        for name in ['distributed_id','incremental']:
            descriptor=getattr(cls,name)
            for access in ['fget','fset']:
                method=getattr(descriptor,access)
                group['signatures'][name+'.'+access]=None if method is None else [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(method).parameters.values()]
        result[module]=group
    if REFERENCE:
        path=Path(importlib.import_module('wandb.sdk.artifacts.artifact').__file__)
        assert path.read_bytes()==(ROOT/'.reference/wandb/source/wandb/sdk/artifacts/artifact.py').read_bytes()
        result['source_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    return result

if __name__=='__main__':print(json.dumps(build()))
