"""Compare public distributed group arguments with pinned transport encoding.

The reference executes its real public Run method and protobuf group assignment;
only unrelated manifest serialization and hosted transport are replaced. Native
calls journal real offline parts. No installed environment is changed.
"""
import hashlib,importlib,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:
 import wandb as w
 from wandb.sdk.interface.interface import InterfaceBase
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
 from backprop.transport import read_journal
module=sys.argv[2] if len(sys.argv)>2 else 'wandb'
Run=importlib.import_module(module).Run
cases=[('none',None),('empty',''),('text','group'),('utf8_bytes','雪'.encode()),('invalid_bytes',b'\xff'),
 ('false',False),('true',True),('zero',0),('one',1),('empty_list',[]),('list',['g']),
 ('empty_dict',{}),('dict',{'g':1}),('float_zero',0.0),('float',1.2)]
output={}
for method in ['upsert_artifact','finish_artifact']:
 for name,group in cases:
  with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory,'WANDB_DATA_DIR':directory+'/data','WANDB_CACHE_DIR':directory+'/cache'}):
   encoded=[]
   if reference:
    run=w.Run(settings=w.Settings(mode='offline',run_id='worker',entity='e',project='p',run_group='fallback',console='off',silent=True))
    boundary=S(_make_artifact_manifest=lambda *a,**k:None)
    def publish(run,artifact,*args,**kwargs):encoded.append(InterfaceBase._make_artifact(boundary,artifact).distributed_id)
    run._interface=S(publish_artifact=publish)
   else:
    run=w.init(id='worker',entity='e',project='p',group='fallback',mode='offline',dir=directory,_deployment={},
     settings={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True},reinit='create_new')
   artifact=w.Artifact('model','model')
   try:
    returned=getattr(Run,method)(run,artifact,distributed_id=group)
    if not reference:encoded=[e['payload']['distributed_id'] for e in read_journal(run.sync_dir) if e['type']=='artifact_part']
    result={'returns_self':returned is artifact,'transport_group':encoded[-1],'local_group':repr(artifact.distributed_id)}
   except Exception as error:result={'error':type(error).__name__,'message':str(error),'local_group':repr(artifact.distributed_id)}
   output[method+'/'+name]=result
   if reference:run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
   else:run.finish()
if reference:
 source=Path(importlib.import_module('wandb.sdk.interface.interface').__file__)
 expected=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb/sdk/interface/interface.py'
 assert source.read_bytes()==expected.read_bytes()
 output['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
print(json.dumps(output))
