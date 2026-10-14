"""Pinned API path/default/validation behavior at the hosted lookup boundary.

Architecture: real Api methods on explicit auth/settings fixture -> native or
pinned path parsing -> one hosted artifact lookup sentinel -> observations.
Main caller: artifact lookup tests. The fixture certifies dispatch/validation;
native persistence tests separately verify actual manifests, bytes and absence.
"""
import hashlib,importlib,inspect,json,os,sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
 import wandb as w
 assert w.__version__=='0.30.0'
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()


def cases():
 paths=['dataset:best','other/dataset:best','team/other/dataset:best','/other/dataset:best',
  'team//dataset:best','//dataset:best','dataset:','dataset','',':best','/', '//',
  'too/many/path/parts:best','dataset:selector/part','other/dataset:selector/part',
  'team/other/dataset:selector:part','training 数据:最近',' dataset : best ','dataset:sha256-content',
  'opaque-identity',None,3,False,b'dataset',[],{},Path('dataset')]
 result={str(index):{'name':name} for index,name in enumerate(paths)}
 for entity in ['configured','',None]:
  for project in ['configured','',None]:
   for name in ['dataset:best','/project/dataset:best','entity//dataset:best']:
    result[repr((entity,project,name))]={'name':name,'entity':entity,'project':project}
 for type in ['data','wrong','',False,7,None]:result['type/'+repr(type)]={'name':'dataset:best','type':type}
 for debug in ['true','false']:
  for name in [None,17,'a/b/c/d:alias']:
   result['debug/'+repr((debug,name))]={'name':name,'debug':debug}
 result['missing']={'args':[]};result['extra']={'args':['dataset:best','data','extra']}
 result['unknown']={'name':'dataset:best','unknown':True}
 result['duplicate']={'args':['dataset:best'],'name':'dataset:best'}
 return result


out={'imports':{}}
for label in ['wandb.Api','wandb.apis.public.Api','wandb.apis.public.api.Api']:
 module,name=label.rsplit('.',1);cls=getattr(importlib.import_module(module),name);group={}
 for key,case in cases().items():
  requests=[];sentinel=SimpleNamespace(type='data')
  api=object.__new__(cls) if REFERENCE else cls._from_store(w.MemorySink())
  api.settings={'entity':case.get('entity','e'),'project':case.get('project','p')}
  if REFERENCE:
   api._default_entity=api.settings['entity']
   api._service_api=SimpleNamespace(execute_graphql=lambda *a,**k:SimpleNamespace(viewer=None))
   def lookup(*,path,service_api):
    requests.append([path.prefix,path.project,path.name]);return sentinel
  else:
   def events(entity,project):
    # Selection gets a matching manifest so the observed path is independent
    # of backend membership existence. No parser is replaced by this fixture.
    value=case.get('name',case.get('args',['dataset:best'])[0] if case.get('args') else 'dataset:best')
    prefix,sep,alias=value.partition(':');root=prefix.split('/')[-1];identity=root+sep+alias
    requests.append([entity,project,identity])
    return [{'run_id':'owner','payload':{'name':root,'version':'sha256-version','aliases':[alias], 'artifact_id':identity}}]
  with ExitStack() as stack:
   stack.enter_context(patch.dict(os.environ,{'WANDB_DEBUG':case.get('debug','false')}))
   if REFERENCE:stack.enter_context(patch.object(w.Artifact,'_from_name',side_effect=lookup))
   else:
    stack.enter_context(patch.object(api,'_artifact_events',side_effect=events))
    stack.enter_context(patch.object(api,'_make_artifact',return_value=sentinel))
   args=case.get('args',[]);kwargs={k:v for k,v in case.items() if k in ['name','type','unknown']}
   try:
    result=api.artifact(*args,**kwargs);observation={'same_return':result is sentinel}
   except Exception as error:
    observation={'error':type(error).__name__,'message':str(error),
                 'cause':type(getattr(error,'exc',None)).__name__}
   observation['lookups']=requests;group[key]=observation
 group['signature']=[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.artifact).parameters.values()]
 out['imports'][label]=group
if REFERENCE:
 out['source_hashes']={}
 for module in ['wandb.apis.public.api','wandb.sdk.artifacts._validators']:
  actual=Path(importlib.import_module(module).__file__);expected=ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')
  assert actual.read_bytes()==expected.read_bytes();out['source_hashes'][module]=hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
