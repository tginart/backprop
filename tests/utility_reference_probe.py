"""Exercise pinned portable helper, Histogram and deprecated public contracts.

Architecture: source-verified isolated SDK -> actual values/files -> terminal
warning recorder -> JSON observations. Main caller: optional utility CI. Absl
and TensorFlow flags use real installed modules when available; old flag objects
exercise the pinned documented legacy shape without importing a fake framework.
"""
import argparse,copy,hashlib,importlib,importlib.util,inspect,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:
 import wandb as w
 assert w.__version__=='0.30.0'
 from wandb.sdk.lib.deprecation import UNSET
 root=Path(inspect.getfile(w)).parent
 for name in ['sdk/wandb_helper.py','sdk/lib/config_util.py','sdk/lib/deprecated_api.py','sdk/wandb_settings.py','sdk/data_types/histogram.py','sdk/data_types/base_types/wb_value.py']:
  expected=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb'/name
  assert hashlib.sha256((root/name).read_bytes()).digest()==hashlib.sha256(expected.read_bytes()).digest()
else:
 import backprop as w
 from backprop.settings import _ANONYMOUS_UNSET as UNSET
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
def outcome(call):
 try:
  value=call()
  if isinstance(value,w.Histogram):value={'decoded_histogram':value.to_json()}
  return {'value':value}
 except Exception as error:return {'error':type(error).__name__}
def signature(value):
 return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(value).parameters.values()]
def warnings_seen(warn):return [{'args':list(c.args),'kwargs':c.kwargs} for c in warn.call_args_list]
output={'histogram':{},'helper':{},'anonymous':{},'ensure_configured':{},'histogram_extension':{}}
for module in ['wandb','wandb.data_types','wandb.sdk.data_types.histogram']:
 cls=importlib.import_module(module).Histogram;result={'signatures':{name:signature(getattr(cls,name)) for name in ['to_json','from_json']},'values':[],'decode':[]}
 for counts,edges in [([1,2],[0,1,2]),((1,2),(0,1,2)),([], [0]),([float('nan'),float('inf')],[0,1,2])]:
  histogram=cls(np_histogram=(counts,edges))
  for args,kwargs in [([],{}),([None],{}),([object()],{}),([] ,{'run':w.Artifact('histogram','data')})]:
   record=histogram.to_json(*args,**kwargs)
   result['values'].append({'json':json.dumps(record),'same_counts':record['values'] is histogram.histogram,'same_edges':record['bins'] is histogram.bins,'return':type(record).__name__})
 result['errors']={name:outcome(call) for name,call in [('extra',lambda:histogram.to_json(1,2)),('unknown',lambda:histogram.to_json(run_or_artifact=None))]}
 for args,kwargs in [([],{}),([{}],{}),([{},None],{}),([] ,{'json_obj':{'values':[1],'bins':[0,1]},'source_artifact':object()}),([None,None],{}),([[],None],{}),([{},None],{'extra':1})]:
  result['decode'].append(outcome(lambda args=args,kwargs=kwargs:cls.from_json(*args,**kwargs)))
 result['registry_decode']=outcome(lambda:cls.init_from_json(cls(np_histogram=([1],[0,1])).to_json(),None))
 extension={'decode':result.pop('decode'),'registry_decode':result.pop('registry_decode'),'roundtrips':[],'invalid':[]}
 for counts,edges in [([1,2],[0,1,2]),((1,2),(0,1,2)),([],[0])]:
  for owner in [None,w.Artifact('decode-owner','dataset'),object()]:
   original=cls(np_histogram=(counts,edges));encoded=original.to_json(owner)
   extension['roundtrips'].append({'encoded':encoded,'decoded':outcome(lambda:cls.from_json(json_obj=encoded,source_artifact=owner))})
 for value in [None,[],{}, {'values':[1]}, {'bins':[0,1]}, {'values':None,'bins':[0]}, {'values':[1,2],'bins':[0,1]}, {'values':[1]*513,'bins':list(range(514))}]:
  extension['invalid'].append(outcome(lambda value=value:cls.from_json(value,None)))
 output['histogram_extension'][module]=extension

 output['histogram'][module]=result
with tempfile.TemporaryDirectory() as directory:
 directory=Path(directory)
 for label,helper in [('root',w.helper),('sdk',importlib.import_module('wandb.sdk.wandb_helper'))]:
  call=helper.parse_config;record={'signature':signature(call),'inputs':{},'yaml':{},'filters':[],'identity':{}}
  class Legacy:
   def __init__(self,parsed):
    self.__dict__['__flags']={'before':1}
    if parsed:self.__dict__['__parsed']=False
   def _parse_flags(self):self.__dict__['__flags']={'after':2}
  values={'dict':{'a':1,'b':2},'namespace':argparse.Namespace(a=1,b=2),'none':None,'list':[1,2],'tuple':(1,2),'int':1,'path':directory/'missing',
      'ordinary_flags_attribute':SimpleNamespace(FLAGS={'a':1}), 'old_flags':Legacy(False),'old_flags_parsed':Legacy(True)}
  for name,value in values.items():record['inputs'][name]=outcome(lambda value=value:call(value))
  for inc,exc in [(None,None),([],[]),(['a'],None),(None,['b']),(['a'],['b']),('ab',None),(None,'a')]:
   params={'a':1,'b':2};value=outcome(lambda:call(params,exclude=exc,include=inc));record['filters'].append({'result':value,'input':params})
  for kind,value in [('dict',{'a':1}),('namespace',argparse.Namespace(a=1))]:
   parsed=call(value);record['identity'][kind]=parsed is (value if kind=='dict' else vars(value))
  for name,text in [('valid','wandb_version: 1\na:\n  value: 3\n  desc: note\n'),('no_version','a: {value: 3}'),('empty',''),('version','wandb_version: 2'),('list','[a, b]'),('missing_value','a: {desc: absent}'),('scalar','a: 3'),('parser','a: [1, 2'),('scanner','a: \"bad\\q\"')]:
   path=directory/(name+'.yaml');path.write_text(text)
   with patch.object(w,'termwarn') as warn:result=outcome(lambda:call(str(path)))
   record['yaml'][name]={'result':result,'warnings':warnings_seen(warn)}
  record['yaml']['missing']=outcome(lambda:call(str(directory/'absent')))
  record['yaml']['directory']=outcome(lambda:call(str(directory)))
  record['errors']={'missing_arg':outcome(lambda:call()),'unknown':outcome(lambda:call({},unknown=True))}
  if importlib.util.find_spec('absl'):
   from absl import flags
   local=flags.FlagValues();flags.DEFINE_integer('count',3,'count',flag_values=local);local(['test','--count=7'])
   record['absl']=call(local)
  if importlib.util.find_spec('tensorflow'):
   import tensorflow as tf
   from tensorflow.python.platform import flags as tf_flags
   # Genuine global flags may include framework/host additions. Select only a
   # declared test flag; the observed type conversion remains the real module.
   tf_flags.DEFINE_integer('backprop_probe_count',9,'count') if 'backprop_probe_count' not in tf_flags.FLAGS else None
   record['tensorflow']=call(tf_flags,include=['backprop_probe_count'])
  output['helper'][label]=record
with patch.object(w,'termwarn') as warn:
 output['ensure_configured']['signature']=signature(w.ensure_configured)
 output['ensure_configured']['calls']=[outcome(w.ensure_configured),outcome(w.ensure_configured)]
 output['ensure_configured']['warnings']=warnings_seen(warn)
 output['ensure_configured']['errors']=[outcome(lambda:w.ensure_configured(1)),outcome(lambda:w.ensure_configured(extra=1))]
for label,cls in [('root',w.Settings),('sdk',importlib.import_module('wandb.sdk.wandb_settings').Settings)]:
 result={}
 for mode in ['default','none','false','allow','dict','unset','shallow','deep','update_none','update_allow','assignment','dict_none','dict_allow','from_settings','env']:
  with patch.object(w,'termwarn') as warn:
   s=cls()
   if mode in ['none','false','allow','dict','unset']:s=cls(anonymous={'none':None,'false':False,'allow':'allow','dict':{'v':1},'unset':UNSET}[mode])
   if mode=='shallow':s=s.model_copy()
   if mode=='deep':s=s.model_copy(deep=True)
   if mode=='update_none':s=s.model_copy(update={'anonymous':None})
   if mode=='update_allow':s=s.model_copy(update={'anonymous':'allow'})
   if mode=='assignment':s.anonymous='allow'
   if mode=='dict_none':s.update_from_dict({'anonymous':None})
   if mode=='dict_allow':s.update_from_dict({'anonymous':'allow'})
   if mode=='from_settings':source=cls(anonymous='allow');warn.reset_mock();s.update_from_settings(source)
   if mode=='env':s.update_from_env_vars({'WANDB_ANONYMOUS':'allow'})
   value=s.anonymous
   result[mode]={'unset':value is UNSET,'value':'opaque' if type(value)==object else value,'dump_has':'anonymous' in s.model_dump(),'included':s.model_dump(include={'anonymous'}),'json':s.model_dump_json(include={'anonymous'}),'explicit':'anonymous' in s.model_fields_set,'warnings':warnings_seen(warn)}

 class Uncopyable:
  def __deepcopy__(self,memo):raise RuntimeError('not copied')
 with patch.object(w,'termwarn'):
  unusual=cls(anonymous=Uncopyable())
  result['excluded_object']={'dump':outcome(lambda:unusual.model_dump(include={'anonymous'})),'json':outcome(lambda:unusual.model_dump_json(include={'anonymous'})),'deep_copy':outcome(lambda:unusual.model_copy(deep=True))}

 with tempfile.TemporaryDirectory() as directory:
  settings_file=Path(directory)/'settings'
  settings_file.write_text('[default]\nanonymous=allow\n')
  with patch.object(w,'termwarn') as warn:
   settings=cls(settings_system=str(settings_file),root_dir=directory)
   settings.update_from_system_settings()
  result['system_file']={'unset':settings.anonymous is UNSET,'explicit':'anonymous' in settings.model_fields_set,'dump_has':'anonymous' in settings.model_dump(),'warnings':warnings_seen(warn)}
 output['anonymous'][label]=result
print(json.dumps(output,sort_keys=True))
