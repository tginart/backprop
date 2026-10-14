"""Compare base value and media serialization with the installed pinned SDK.

Architecture: real public imports and value methods -> local file fixtures ->
recorded owner calls. Run/artifact owners supply deterministic service-free
responses; their storage implementations are covered by native journal tests.
Main caller: test_optional_media_serialization. No network is used here.
"""
import copy
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

REFERENCE=sys.argv[1]=='reference'
ROOT=Path(__file__).resolve().parents[1]
if REFERENCE:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
base=importlib.import_module('wandb.data_types')
if REFERENCE:
    for cls,digest in [(base.WBValue,'083f4db030911ce160d991bdbd61542f1ffa6f8c90925a948381bc90e7500ae2'),
            (base.Media,'9c2ea72e84eb2b07c91ddaa4e5ca94e70aff0c25ede272bf5ccc257efe61f856')]:
        assert hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()==digest

def signature(fn):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(fn).parameters.values()]

def attempt(call):
    try:return {'value':call()}
    except Exception as error:return {'error':type(error).__name__,'message':str(error)}

def value_contract(cls):
    first,second=cls(),cls()
    class Equal(cls):
        def __eq__(self,other):return isinstance(other,Equal)
    class Child(cls):pass
    cases={'state':vars(first),'identity':[first==first,first!=first,first==second,first!=second,first==object(),first!=object()],
        'hash':attempt(lambda:hash(first)), 'copy':[copy.copy(first)==first,copy.deepcopy(first)==first],
        'override_ne':Equal()!=Equal(), 'child':[Child()==Child(),attempt(lambda:hash(Child()))],
        'positional_error':attempt(lambda:cls(1)), 'keyword_error':attempt(lambda:cls(extra=1)),
        'to_data_array':attempt(first.to_data_array),'array_positional':attempt(lambda:first.to_data_array(1)),
        'array_keyword':attempt(lambda:first.to_data_array(extra=1)),
        'to_json':attempt(lambda:first.to_json(None)), 'from_json':attempt(lambda:cls.from_json({},None))}
    return {'signature':signature(cls),'array_signature':signature(cls.to_data_array),'cases':cases}

def suffix_contract(cls):
    cases={}
    for label,args,kwargs in [('default',('name',),{}),('empty',('',),{}),('existing',(cls.with_suffix('name'),),{}),
        ('custom',('name','txt'),{}),('keyword',(),{'name':'name','filetype':'bin'}),('empty_type',('name',''),{}),
        ('integer',(3,),{}),('none',(None,),{}),('path',(Path('name'),),{}),('bytes',(b'name',),{}),
        ('list',([],),{}),('bad_type',('name',None),{}),('missing',(),{}),('too_many',('x','y','z'),{}),
        ('extra',('x',),{'extra':True}),('duplicate',('x',),{'name':'y'})]:
        cases[label]=attempt(lambda:cls.with_suffix(*args,**kwargs))
    class Blank(cls):_log_type=''
    cases['blank_log_type']=Blank.with_suffix('name')
    return {'signature':signature(cls.with_suffix),'cases':cases}

class OwnerRun(w.Run):
    def __init__(self,directory):object.__setattr__(self,'_directory',directory)
    @property
    def dir(self):return self._directory

class Entry:
    def __init__(self,path):self.path=path
    def ref_url(self):return 'fixture-artifact://source/'+self.path

class Destination(w.Artifact):
    def __init__(self,existing=None,fail=False):
        self.calls=[];self.existing=existing;self.fail=fail
    def get_added_local_path_name(self,path):return self.existing
    def add_file(self,path,name=None,is_tmp=False):
        self.calls.append(['file',Path(path).name,name,is_tmp]);return Entry(name)
    def add_reference(self,uri,name=None):
        self.calls.append(['reference',uri if isinstance(uri,str) else uri.ref_url(),name])
        if self.fail:raise OSError('reference storage unavailable')
        return [Entry(name)]

def media_contract(cls,directory):
    class FileMedia(cls):
        _log_type='base-serialization'
        def __init__(self,path,caption=None,temporary=False):super().__init__(caption);self._set_file(str(path),is_tmp=temporary)
        @classmethod
        def get_media_subdir(cls):return 'media/base'
    path=directory/'payload.bin';path.write_bytes(b'fixed-payload')
    run=OwnerRun(str(directory));other=OwnerRun(str(directory/'other'))
    cases={}
    for label,caption in [('no_caption',None),('empty_caption',''),('caption','hello'),('raw_caption',{'x':1})]:
        value=FileMedia(path,caption)
        cases[label+'_unknown_owner']=value.to_json(object())
        value._run=run
        cases[label+'_bound']=value.to_json(run)
    value=FileMedia(path)
    cases['unbound']=attempt(lambda:value.to_json(run))
    value._run=other;cases['cross_run']=attempt(lambda:value.to_json(run))
    for label,reference,latest,bound in [('source','fixture-artifact://source/item',None,False),('latest_only',None,'fixture-artifact://source/item?latest',False),
            ('both','fixture-artifact://source/item','fixture-artifact://source/item?latest',False),('bound_source','fixture-artifact://source/item',None,True)]:
        value=FileMedia(path);value._run=run if bound else None
        value._get_artifact_entry_ref_url=lambda:reference;value._get_artifact_entry_latest_ref_url=lambda:latest
        cases[label]=attempt(lambda:value.to_json(run))
    for label,temporary,existing in [('file',False,None),('temporary',True,None),('existing',False,'already/present.bin')]:
        target=Destination(existing);value=FileMedia(path,'caption',temporary)
        cases[label]={'json':value.to_json(target),'calls':target.calls}
    for kind in [w.Audio,w.Image]:
        target=Destination();value=kind('https://example.test/media-file')
        cases[kind.__name__+'_reference']={'json':cls.to_json(value,target),'calls':target.calls}
    cases['empty_artifact']=cls('empty').to_json(Destination())
    for label,inside,existing,fail in [('source_regular',False,None,False),('source_downloaded',True,None,False),
            ('source_existing',True,'already.bin',False),('source_error',True,None,True)]:
        location=directory/'download'/'nested'/'source.bin' if inside else path
        location.parent.mkdir(parents=True,exist_ok=True);location.write_bytes(b'fixed-payload')
        source=SimpleNamespace(_default_root=lambda:str(directory/'download'),get_entry=lambda name:Entry(name))
        value=FileMedia(location,'source');value._set_artifact_source(source)
        target=Destination(existing,fail)
        cases[label]={'result':attempt(lambda:value.to_json(target)),'calls':target.calls}
    cases['missing_owner']=attempt(lambda:FileMedia(path).to_json())
    cases['wrong_keyword']=attempt(lambda:FileMedia(path).to_json(owner=run))
    cases['extra_positional']=attempt(lambda:FileMedia(path).to_json(run,run))
    return {'signature':signature(cls.to_json),'cases':cases}

observations={'values':{},'suffixes':{},'media':{},'arrays':{}}
for module in ['wandb.data_types','wandb.sdk.data_types.base_types.wb_value']:
    observations['values'][module]=value_contract(importlib.import_module(module).WBValue)
contracts=json.loads((ROOT/'tests/fixtures/media-contract-apis.json').read_text())
for api in contracts['suffixes']:
    value=w
    for part in api.split('.')[1:-1]:value=getattr(value,part)
    observations['suffixes'][api]=suffix_contract(value)
for api in contracts['arrays']:
    cls=w
    for part in api.split('.')[1:-1]:cls=getattr(cls,part)
    value=object.__new__(cls)
    observations['arrays'][api]={'signature':signature(cls.to_data_array),
        'cases':{'default':attempt(value.to_data_array),'extra':attempt(lambda:value.to_data_array(1)),
            'keyword':attempt(lambda:value.to_data_array(extra=True))}}
with tempfile.TemporaryDirectory() as directory:
    for module in ['wandb.data_types','wandb.sdk.data_types.base_types.media']:
        for name in ['Media','BatchableMedia']:
            observations['media'][module+'.'+name]=media_contract(getattr(importlib.import_module(module),name),Path(directory))
print(json.dumps(observations,sort_keys=True))
