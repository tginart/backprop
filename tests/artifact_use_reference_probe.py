"""Real decorated use_artifact calls with explicit hosted publication fixtures.

Architecture: genuine Run/Artifact constructors -> actual preparation and
validation -> hosted lookup/commit acknowledgement boundary -> observations.
Main caller: artifact-use contract tests. Native cases use real journals except
where a failure is explicitly injected at the same boundary on both sides.
"""
import hashlib,importlib,inspect,json,os,sys,tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
 import wandb as w
 from wandb.sdk.artifacts.artifact import ArtifactState
 assert w.__version__=='0.30.0'
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()


def cases():
 result={}
 for stage in ['draft','finalized','queued','committed','name']:
  for label,options in {'default':{},'empty':{'aliases':[]},'custom':{'aliases':['release']},
   'string':{'aliases':'release'},'invalid':{'aliases':['bad/name']},'wrong_type':{'type':'wrong'},
   'use_as':{'use_as':'input'},'empty_use_as':{'use_as':''},'all':{'type':'data','aliases':['best'],'use_as':'input'}}.items():
   result[stage+'/'+label]={'stage':stage,**options}
 for mode in ['offline','finished','disabled']:
  for stage in ['draft','committed','name','invalid']:
   result[mode+'/'+stage]={'stage':stage,'mode':mode,'use_as':'ignored'}
 for label,value in [('none',None),('integer',7),('bytes',b'dataset'),('path',Path('dataset')),('list',[])]:
  result['invalid/'+label]={'stage':'invalid','value':value,'use_as':'warn'}
 for mode in ['online','offline','finished','disabled']:
  for call in ['missing','extra','unknown','duplicate']:
   result[mode+'/'+call]={'stage':'committed','mode':mode,'call':call}
 result['draft/commit_failure']={'stage':'draft','wait_failure':True}
 result['name/lookup_failure']={'stage':'name','lookup_failure':True}
 result['offline_producer']={'stage':'offline_queued'}
 for stage in ['draft','queued','offline_queued']:
  result[stage+'/existing_type']={'stage':stage,'expected_type':'wrong'}
  result[stage+'/invalid_alias_before_type']={'stage':stage,'expected_type':'wrong','aliases':['bad/name']}
 return result


def constructor(cls,mode,directory):
 settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}
 if REFERENCE:
  if mode=='disabled':return w.init(mode='disabled',settings=settings)
  run=cls(settings=w.Settings(mode='offline' if mode=='offline' else 'online',run_id='reader',entity='e',project='p',**settings))
  importlib.import_module('wandb.sdk.wandb_init')._set_global_run(run);return run
 return w.init(id='reader',entity='e',project='p',mode='offline' if mode=='offline' else 'disabled' if mode=='disabled' else 'online',dir=directory,
  _sink=None if mode in ['offline','disabled'] else w.MemorySink(),_deployment={},settings={**settings,'flush_interval':600})


def finish(run):
 if REFERENCE:
  run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
 else:run.finish()


out={'imports':{}}
for label in ['wandb.Run','wandb.sdk.wandb_run.Run','wandb.use_artifact']:
 module,member=label.rsplit('.',1);cls=getattr(importlib.import_module(module),member) if member=='Run' else w.Run;group={}
 for key,case in cases().items():
  with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}),ExitStack() as stack:
   mode=case.get('mode','online');run=constructor(cls,mode,directory);artifact=w.Artifact('dataset','data');stage=case['stage']
   if stage=='finalized':artifact.finalize()
   if REFERENCE:
    if stage in ['committed','name']:
     artifact._state=ArtifactState.COMMITTED;artifact._id='identity';artifact._aliases=['best','latest']
     artifact._entity=artifact._source_entity='e';artifact._project=artifact._source_project='p'
     artifact._name=artifact._source_name='dataset:v0';artifact._version=artifact._source_version='v0'
    elif stage=='queued':artifact._aliases=['best','latest']
   elif stage in ['committed','name','queued'] and mode not in ['offline','disabled']:
    run.log_artifact(artifact,aliases=['best'])
    if stage!='queued':run.flush()
   elif stage=='offline_queued':
    offline=w.init(id='offline-producer',entity='e',project='p',mode='offline',dir=directory,reinit='create_new',_deployment={},settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True})
    offline.log_artifact(artifact,aliases=['best']);offline.finish()
   elif stage in ['committed','name']:
    artifact._logged=True;artifact._committed=True;artifact._aliases=['best','latest'];artifact._version='sha256-value'
    artifact._entity='e';artifact._project='p'
   warnings=[];lookups=[];uses=[];published=[];waits=[]
   stack.enter_context(patch.object(w,'termwarn',side_effect=lambda text,**kwargs:warnings.append({'message':text,**kwargs})))
   def lookup(name=None,type=None):
    lookups.append({'name':name,'type':type})
    if case.get('lookup_failure'):raise w.CommError('lookup unavailable')
    return artifact
   if REFERENCE:
    def deliver(owner,a,aliases,tags,*args,**kwargs):
     published.append(list(aliases));return object()
    api=SimpleNamespace(_artifact=lookup,_service_api=object(),settings={'entity':'e','project':'p'},
      _expected_artifact_type=lambda **kwargs:case.get('expected_type'),_set_artifact_save_handle=lambda *args:None)
    run._cached_public_api=api
    run._interface=SimpleNamespace(deliver_artifact=deliver,publish_use_artifact=lambda value:None,
     deliver_history_step=lambda:SimpleNamespace(wait_or=lambda **kwargs:SimpleNamespace(step=0)))
    def wait(timeout=None):
     waits.append(True)
     if case.get('wait_failure'):raise OSError('commit unavailable')
     artifact._state=ArtifactState.COMMITTED;artifact._id='identity'
     artifact._aliases=sorted(set(artifact._aliases).union(*(set(value) for value in published)))
     artifact._entity=artifact._source_entity='e';artifact._project=artifact._source_project='p'
     artifact._name=artifact._source_name='dataset:v0';artifact._version=artifact._source_version='v0'
     return artifact
    stack.enter_context(patch.object(artifact,'wait',side_effect=wait))
    stack.enter_context(patch('wandb.sdk.artifacts._gqlutils.record_artifact_use',side_effect=lambda *a,**k:uses.append(k)))
   else:
    stack.enter_context(patch.object(w.Api,'artifact',side_effect=lookup))
    original=artifact.wait
    def wait(timeout=None):
     waits.append(True)
     if case.get('wait_failure'):raise OSError('commit unavailable')
     return original(timeout)
    stack.enter_context(patch.object(artifact,'wait',side_effect=wait))
   if REFERENCE and stage=='offline_queued':
    # Execute genuine offline publication; it freezes the draft but does not
    # assign its future service alias list. Only the service boundary is faked.
    offline=cls(settings=w.Settings(mode='offline',run_id='offline-producer',entity='e',project='p',silent=True,console='off',disable_git=True,x_disable_stats=True))
    source_publications=[]
    offline._interface=SimpleNamespace(publish_artifact=lambda *args,**kwargs:source_publications.append((args,kwargs)))
    offline.log_artifact(artifact,aliases=['best'])
    assert len(source_publications)==1 and artifact.is_draft()
    offline._is_finished=True
   if not REFERENCE and case.get('expected_type'):
    run._sink.events.append({'schema_version':1,'event_id':'existing-type','run_id':'other','timestamp':'2026-10-01T00:00:00+00:00',
     'entity':'e','project':'p','type':'artifact','payload':{'artifact_id':'prior','name':'dataset','artifact_type':case['expected_type'],
     'version':'sha256-prior','files':[],'aliases':[]}})
   if mode=='finished':finish(run)
   value=case.get('value') if stage=='invalid' else 'dataset:best' if stage=='name' else artifact
   args=[value];kwargs={k:v for k,v in case.items() if k in ['type','aliases','use_as']}
   if case.get('call')=='missing':args=[]
   elif case.get('call')=='extra':args=[value,None,None,None,'extra']
   elif case.get('call')=='unknown':kwargs['unknown']=True
   elif case.get('call')=='duplicate':kwargs['artifact_or_name']=value
   call=w.use_artifact if member=='use_artifact' else run.use_artifact
   try:
    result=call(*args,**kwargs)
    observed={'same_return':result is artifact,'none_return':result is None}
    if result is not None:observed.update(state=result.state,aliases=sorted(result.aliases),type=result.type)
   except Exception as error:
    kind='Error' if type(error) is w.Error else type(error).__name__
    observed={'error':kind,'message':str(error).replace(artifact.name,'<artifact>')}
   observed.update(warnings=warnings,lookups=lookups,wait_count=len(waits))
   # Draft relationships are recorded by W&B's commit service. Native journals
   # own that write directly; verify actual relationship bytes separately.
   if REFERENCE:observed['recorded_input']=bool(uses or waits and observed.get('same_return'))
   else:observed['recorded_input']=any(e['type']=='artifact_use' for e in __import__('backprop.transport',fromlist=['read_journal']).read_journal(run.sync_dir)) if not run.disabled else False
   group[key]=observed
   if mode!='finished':finish(run)
 group['signature']=[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.use_artifact).parameters.values()]
 out['imports'][label]=group
# Offline config capture is an explicit native extension. Observe the pinned
# rejection separately instead of hiding it inside otherwise-equal branches.
out['offline_config']={}
for label in ['wandb.Run','wandb.sdk.wandb_run.Run']:
 module,member=label.rsplit('.',1);cls=getattr(importlib.import_module(module),member)
 for method in ['__setitem__','__setattr__','update','setdefaults','update_locked','merge_locked']:
  with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
   run=constructor(cls,'offline',directory);artifact=w.Artifact('dataset','data')
   try:
    call=getattr(run.config,method)
    call('dataset',artifact) if method.startswith('__') else call({'dataset':artifact})
    observed={'accepted':True,'state':artifact.state,'identity_preserved':run.config['dataset'] is artifact}
   except Exception as error:observed={'error':type(error).__name__,'message':str(error)}
   finally:finish(run)
   out['offline_config'][label+'/'+method]=observed
if REFERENCE:
 out['source_hashes']={}
 for name in ['wandb.sdk.wandb_run','wandb.sdk.artifacts.artifact','wandb.sdk.lib.module','wandb.sdk.lib.noop_run','wandb.sdk.wandb_config']:
  actual=Path(importlib.import_module(name).__file__);expected=ROOT/'.reference/wandb/source'/Path(*name.split('.')).with_suffix('.py')
  assert actual.read_bytes()==expected.read_bytes();out['source_hashes'][name]=hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
