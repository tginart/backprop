"""Actual pinned alias property and artifact-preparation behavior.

Architecture: genuine constructors -> explicit publication-state fixture ->
public getter/setter and the real logging preparation method -> observations.
Main caller: artifact alias contract tests. Only service-owned state is injected;
no validator, property or alias preparation behavior is replaced.
"""
import hashlib,importlib,inspect,json,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
 import wandb as w
 from wandb.sdk.artifacts.artifact import ArtifactState
 assert w.__version__=='0.30.0'
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()


def fresh(cls,state):
 a=cls('dataset','data')
 if state!='draft':a.finalize()
 if state in ['queued','committed']:
  a._id='artifact-id'
  if REFERENCE:a._state=ArtifactState.COMMITTED if state=='committed' else ArtifactState.PENDING
  else:a._logged=True;a._committed=state=='committed'
 return a


def plain(value):
 if isinstance(value,bytes):return {'python_bytes':value.hex()}
 if isinstance(value,(list,tuple)):return [plain(item) for item in value]
 if isinstance(value,dict):return {key:plain(item) for key,item in value.items()}
 return value


def inputs():
 return {'empty':[],'empty_alias':[''],'version_alias':['v1'],'unicode_version':['v١'],
  'default_name':['latest'],'duplicates':['best','best','latest','best'],'string':'best','empty_string':'',
  'tuple':('best','dev'),'generator':(x for x in ['dev','best']),'mapping':{'dev':1,'best':2},
  'spaces':[' first best '],'unicode':['训练'],'case':['Best','best'],
  'slash':['bad/name'],'colon':['bad:alias'],'mixed_invalid':['good','bad/name'],
  'bytes':b'best','nested_bytes':[b'best'],'none':None,'int':3,'numbers':[3],
  'nested':[['best']],'backslash':['back\\slash'],'newline':['one\ntwo'],
  'content_version':['sha256-abcdef'],'hyphen':['v-1'],'version_zero':['v0']}


def attempt(a,call):
 try:return {'value':plain(call())}
 except Exception as error:return {'error':type(error).__name__,'message':str(error),
  'attribute_error':isinstance(error,AttributeError),'name':getattr(error,'name',None),'owns_object':getattr(error,'obj',None) is a}


out={'imports':{},'logging_aliases':{}}
for prefix in ['wandb','wandb.sdk','wandb.sdk.artifacts.artifact']:
 cls=importlib.import_module(prefix).Artifact;cases={}
 for state in ['draft','finalized','queued','committed']:
  a=fresh(cls,state);cases[state+'/get']=attempt(a,lambda:a.aliases)
  for name,value in inputs().items():
   a=fresh(cls,state);a._aliases=['original']
   def assign():
    a.aliases=value
    return a.aliases
   cases[state+'/'+name]=attempt(a,assign)
   cases[state+'/'+name]['stored']=plain(a._aliases)
  a=fresh(cls,state)
  def mutate():
   value=['best'];a.aliases=value;value.append('external');a.aliases.append('inplace')
   return {'aliases':a.aliases,'input':value,'same_object':a.aliases is a.aliases}
  cases[state+'/mutation']=attempt(a,mutate)
 descriptor=cls.aliases
 cases['getter_signature']=[(p.name,p.kind.name) for p in inspect.signature(descriptor.fget).parameters.values()]
 cases['setter_signature']=[(p.name,p.kind.name) for p in inspect.signature(descriptor.fset).parameters.values()]
 out['imports'][prefix]=cases
for name,value in inputs().items():
 a=w.Artifact('dataset','data')
 if REFERENCE:
  from wandb.sdk.artifacts._validators import validate_aliases
  run=object.__new__(w.sdk.wandb_run.Run);run._settings=SimpleNamespace(run_id='run')
  def prepare():
   validated=None if value is None else validate_aliases(value)
   artifact,aliases=run._prepare_artifact(a,aliases=validated)
   return aliases
 else:
  def prepare():
   run=SimpleNamespace(_journal=None,step=0)
   return a._prepare_log(run,aliases=value)['aliases']
 result=attempt(a,prepare)
 # The upstream helper returns set iteration order. Compare membership rather
 # than pretending that order is stable across independently seeded processes.
 if 'value' in result:result['value']=sorted(result['value'],key=lambda item:json.dumps(item,sort_keys=True))
 out['logging_aliases'][name]=result
def use_existing_cases():
 """Compare alias dispatch for real methods on already committed inputs.

 The pinned method body runs with only service lookup/relationship recording
 replaced at their hosted boundaries. Native cases use real MemorySink runs.
 Whole-Run decorators and unrelated use_artifact arguments are not certified.
 """
 import tempfile,os
 from unittest.mock import patch
 from contextlib import ExitStack
 result={}
 for kind in ['object','name']:
  for label,aliases in {'none':None,'empty':[],'other':['other'],'string':'other','invalid':['bad/name'],'version':['v1'],'empty_alias':['']}.items():
   records=[]
   if REFERENCE:
    a=fresh(w.Artifact,'committed');a._aliases=['best','latest'];a._entity='e';a._project='aliases'
    service=object();api=SimpleNamespace(_artifact=lambda **_:a,_service_api=service)
    run=SimpleNamespace(_settings=SimpleNamespace(_offline=False,entity='e',project='aliases',run_id='consumer'),
      _public_api=lambda:api,_interface=None)
    call=inspect.unwrap(w.sdk.wandb_run.Run.use_artifact)
    with patch('wandb.sdk.artifacts._gqlutils.record_artifact_use',side_effect=lambda *args,**kwargs:records.append(kwargs)):
     value=call(run,a if kind=='object' else 'dataset:best',aliases=aliases)
    result[kind+'/'+label]={'aliases':sorted(value.aliases),'uses':len(records),'state':value.state}
   else:
    settings={'silent':True,'disable_git':True,'console':'off','x_disable_stats':True,'flush_interval':600}
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
     sink=w.MemorySink()
     with w.init(id='consumer',entity='e',project='aliases',dir=directory,_sink=sink,_deployment={},settings=settings) as run:
      a=w.Artifact('dataset','data');run.log_artifact(a,aliases=['best']);run.flush()
      value=run.use_artifact(a if kind=='object' else 'dataset:best',aliases=aliases)
     result[kind+'/'+label]={'aliases':sorted(value.aliases),'uses':sum(e['type']=='artifact_use' for e in sink.events),'state':value.state}
 return result

out['use_existing']=use_existing_cases()

if REFERENCE:
 out['source_hashes']={}
 for module in ['wandb.sdk.artifacts.artifact','wandb.sdk.artifacts._validators','wandb.util','wandb.sdk.wandb_run']:
  actual=Path(importlib.import_module(module).__file__);expected=ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')
  assert actual.read_bytes()==expected.read_bytes()
  out['source_hashes'][module]=hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
