"""Compare Graph files and explicit native repairs in isolated SDK processes.

Architecture: genuine pinned graph code -> real files -> recording Run boundary
-> observed descriptors, snapshots and errors. Main caller: optional Graph file
contracts. Pprint and repeated-binding defects are recorded before comparing the
intended repaired result. No graph serialization methods are replaced.
"""
import contextlib,hashlib,importlib,inspect,io,json,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:
 import wandb as w
 assert w.__version__=='0.30.0'
 source=Path(inspect.getfile(w.Graph));expected=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb/sdk/data_types/graph.py'
 assert hashlib.sha256(source.read_bytes()).hexdigest()==hashlib.sha256(expected.read_bytes()).hexdigest()
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
def outcome(call):
 try:return {'value':call()}
 except Exception as e:return {'error':type(e).__name__}
def signature(call):
 return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]
class ProbeRun:
 def __init__(self,directory,link=False,symlink=False):
  self.dir=str(directory);self.published=[];self._settings=SimpleNamespace(allow_media_symlink=link,symlink=symlink)
 def _publish_file(self,path):self.published.append(path)
def graph(cls=None):
 g=(cls or w.Graph)(format='custom');a=g.add_node(id='a',name='source',output_shape=[None,2]);b=g.add_node(id='b',name='sink',num_parameters=3);g.add_edge(a,b);return g
def state(g):return [g.file_is_set(),g.is_bound()]
def printed(g):
 out=io.StringIO()
 with contextlib.redirect_stdout(out):value=g.pprint()
 return {'return':value,'text':out.getvalue()}
out={'portable':{'aliases':{},'binding':[],'values':{},'errors':{},'helpers':{}},'repairs':{}}
with tempfile.TemporaryDirectory() as directory:
 directory=Path(directory);boundary=patch.object(w,'Run',ProbeRun) if reference else patch.object(importlib.import_module('backprop.run'),'Run',ProbeRun)
 with boundary:
  for module in ['wandb','wandb.data_types','wandb.sdk.data_types.graph']:
   cls=importlib.import_module(module).Graph;g=graph(cls);run=ProbeRun(directory/module)
   before={'state':state(g),'unbound':outcome(lambda:g.to_json(run)),'invalid_owner':outcome(lambda:g.to_json(None))}
   g.bind_to_run(run,'graph',2,id_='chosen');artifact=w.Artifact('graph-'+module.replace('.','-'),'dataset')
   out['portable']['aliases'][module]={'same':cls is w.Graph,'signatures':{name:signature(getattr(cls,name)) for name in ['bind_to_run','to_json','from_json','path_is_reference','captions','pprint','from_keras']},'before':before,'after':state(g),'descriptor':g.to_json(run),'artifact':g.to_json(artifact),'file':Path(g._path).read_text(),'captions':cls.captions([g]),'reference':cls.path_is_reference('s3://bucket/file')}
   out['repairs'][module]={'pprint':outcome(lambda:printed(g))}
   decoded=cls.from_json({'path':'graph'},SimpleNamespace(get_entry=lambda _:SimpleNamespace(download=lambda:g._path)))
   out['repairs'][module]['decoded']={'format_is_filename':decoded.format==g._path,'state':state(decoded),'nodes':len(decoded.nodes),'edges':len(decoded.edges)}
   if reference:
    from wandb.sdk.data_types.graph import Node,Edge
    with patch.object(Node,'attributes',property(lambda n:n._attributes),create=True),patch.object(Edge,'attributes',property(lambda e:e._attributes),create=True):
     intended=printed(g)
   else:intended=printed(g)
   out['portable']['aliases'][module]['pprint_intended']=intended
   first=Path(g._path);g.add_node(id='late');g.bind_to_run(run,'ignored',3,id_='other')
   out['portable']['aliases'][module]['rebound_values']=json.loads(Path(g._path).read_text())
   out['repairs'][module]['rebound']={'published':len(run.published),'same_path':Path(g._path)==first,'inside_owner':Path(g._path).is_relative_to(run.dir)}
  for i,(identity,ignore,link,symlink) in enumerate([(None,None,False,False),(0,False,False,False),('chosen',True,False,False),('hard',True,True,False),('sym',True,True,True)]):
   g=graph();run=ProbeRun(directory/f'bind-{i}',link,symlink);other=ProbeRun(directory/f'other-{i}')
   value=g.bind_to_run(run,'/../nested/graph','step',id_=identity,ignore_copy_err=ignore)
   out['portable']['binding'].append({'return':value,'descriptor':g.to_json(run),'artifact':g.to_json(w.Artifact('binding-'+str(i),'dataset')),'published':run.published,'file':Path(g._path).read_text(),'wrong_owner':outcome(lambda:g.to_json(other))})
  from datetime import date,datetime,timezone
  import enum
  class Choice(enum.Enum):ONE=1
  class Custom:
   def json_encode(self):return {'custom':True}
  values={'scalar':1,'tuple':(1,2),'set':{1,2},'bytes':b'ab','nan':float('nan'),'inf':float('inf'),'date':date(2025,1,2),'datetime':datetime(2025,1,2,tzinfo=timezone.utc),'enum':Choice.ONE,'custom':Custom(),'media':w.Html('example'),'invalid':object()}
  try:import numpy as np
  except ImportError:pass
  else:values.update(array=np.arange(40),zero_dim=np.array(3),scalar32=np.float32(1.5))
  for name,value in values.items():
   def encode():
    g=graph();g.nodes[0]._attributes['value']=value;g.bind_to_run(ProbeRun(directory/name),'value',0,id_='value');return Path(g._path).read_text()
   out['portable']['values'][name]=outcome(encode)
  for name,args,kwargs in [('missing',[],{}),('no_run',[None,'x',0],{}),('invalid_owner',[object(),'x',0],{}),('extra',[ProbeRun(directory/'extra'),'x',0],{'unknown':True})]:
   out['portable']['errors'][name]=outcome(lambda args=args,kwargs=kwargs:graph().bind_to_run(*args,**kwargs))
  g=graph();g._caption='named'
  out['portable']['helpers']['paths']=[outcome(lambda v=v:w.Graph.path_is_reference(v)) for v in [None,Path('http://a'),'s3://a','gs://a','http://a','https://a','ftp://a','file:///a','',{},1,b'http://a']]
  out['portable']['helpers']['captions']=[outcome(lambda x=x:w.Graph.captions(x)) for x in [[],[graph()],[g,graph()],None,[object()]]]
print(json.dumps(out,sort_keys=True))
