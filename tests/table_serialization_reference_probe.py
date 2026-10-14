"""Execute public Table serializers against the installed pinned SDK.

Architecture: isolated SDK -> real artifacts/file bytes -> source-owner fixture
for local downloads -> signatures, schemas, mutations, rows and error results.
The fixture substitutes unavailable service reads only; value/type methods are
never replaced. Physical random storage names are normalized by file purpose.
Main caller: test_optional_table_serialization. No network or installs.
"""
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from datetime import date,datetime,timezone
from decimal import Decimal
from collections import OrderedDict

reference=sys.argv[1]=='reference'
if reference:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


from wandb.sdk.data_types.base_types.wb_value import WBValue
from wandb.sdk.data_types import _dtypes


class ProbeValue(WBValue):
    _log_type='backprop-public-probe-value'

    def __init__(self,value):super().__init__();self.value=value

    def to_json(self,artifact):
        assert isinstance(artifact,w.Artifact)
        if 'custom-value.txt' not in artifact.manifest.entries:
            with artifact.new_file('custom-value.txt',mode='w') as stream:stream.write(str(self.value))
        return {'_type':self._log_type,'path':'custom-value.txt'}

    @classmethod
    def from_json(cls,value,artifact):
        return cls(int(Path(artifact.get_entry(value['path']).download()).read_text()))


class ProbeType(_dtypes.Type):
    name='backprop-public-probe-type'

    def __init__(self,minimum=3):self.params['minimum']=minimum

    def assign(self,value):return self if isinstance(value,int) and value>=self.params['minimum'] else _dtypes.InvalidType()

    def to_json(self,artifact=None):
        assert isinstance(artifact,w.Artifact)
        if 'custom-type.txt' not in artifact.manifest.entries:
            with artifact.new_file('custom-type.txt',mode='w') as stream:stream.write(str(self.params['minimum']))
        return {'wb_type':self.name,'params':{'path':'custom-type.txt'}}

    @classmethod
    def from_json(cls,value,artifact=None):
        return cls(int(Path(artifact.get_entry(value['params']['path']).download()).read_text()))


_dtypes.TypeRegistry.add(ProbeType)

def simple(value):
    if isinstance(value,ProbeValue):return {'custom_value':value.value}
    if hasattr(value,'shape') and hasattr(value,'dtype') and hasattr(value,'tolist'):
        return {'numpy':simple(value.tolist()),'shape':list(value.shape),'dtype':str(value.dtype)}
    if hasattr(value,'tolist'):return simple(value.tolist())
    if isinstance(value,(date,datetime)):return {'datetime':value.isoformat()}
    if isinstance(value,dict):return {str(k):simple(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [simple(v) for v in value]
    if isinstance(value,(str,int,float,bool)) or value is None:
        if isinstance(value,str) and value.endswith('.npz'):return '<array-file>.npz'
        if isinstance(value,str) and value.endswith('.table.json'):return '<table-file>.table.json'
        return value
    return {'class':type(value).__name__}


def outcome(call):
    try:return {'value':simple(call())}
    except Exception as error:return {'error':'Error' if type(error) is w.Error else type(error).__name__}


def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]


class ProbeRun:
    def __init__(self,path):
        self.dir=str(path);self._settings=SimpleNamespace(allow_media_symlink=False,symlink=False);self.published=[]
        self._telemetry_obj=SimpleNamespace(feature=SimpleNamespace());self._telemetry_callback=lambda obj:None
    def _publish_file(self,path):self.published.append(path)


class Source:
    def __init__(self,artifact):
        self.artifact=artifact;self.aliases=['latest']
        # Only authenticated downloads are substituted. The manifest bytes and
        # all serializers were produced by the real artifact implementation.
        self.manifest=SimpleNamespace(entries={path:SimpleNamespace(local_path=entry.local_path,download=lambda entry=entry:entry.local_path) for path,entry in artifact.manifest.entries.items()})
    def get_entry(self,path):
        entry=self.manifest.entries[path]
        local=entry.local_path
        return SimpleNamespace(download=lambda:local)
    def get(self,path):
        candidates=[path,path+'.table.json'];selected=next((name for name in candidates if name in self.manifest.entries),None)
        if selected is None:return None
        return w.Table.from_json(json.loads(Path(self.get_entry(selected).download()).read_text()),self)


out={'aliases':{},'cases':{},'invalid':{}}
if reference:out['source_sha256']=hashlib.sha256(Path(inspect.getfile(w.Table)).read_bytes()).hexdigest()
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    for module in ['wandb','wandb.data_types','wandb.sdk.data_types.table']:
        cls=getattr(importlib.import_module(module),'Table');artifact=w.Artifact('alias','dataset')
        table=cls(columns=['x'],data=[[1]])
        descriptor=table.to_json(run_or_artifact=artifact)
        restored=cls.from_json(json_obj=descriptor,source_artifact=Source(artifact))
        out['aliases'][module]={'signatures':{name:signature(getattr(cls,name)) for name in ['to_json','from_json']},'json':simple(descriptor),'rows':simple(restored.data),'schema':simple(restored._column_types.to_json(artifact)),'same_class':type(restored) is cls}
    cases={'empty':([],None),'scalars':([[1],[-2],[None]],None),'dates':([[date(2024,1,1)],[datetime(2024,1,1,12,0,0,123456)]],None),
           'containers':([[{'a':(1,2)}]],True),'decimal':([[Decimal('1.2')]],True),'nested_table':([[w.Table(columns=['nested'],data=[[1]])]],None),
           'custom-value':([[ProbeValue(7)]],True),'integer_column':([[1]],None),'html':([[w.Html('<p>table</p>',inject=False)]],None)}
    try:
        import numpy as np
        for shape in [(2,),(150,),(151,),(2,2),(0,2),()]:cases['array-'+str(shape)]=([[np.zeros(shape)],[np.ones(shape)]],None)
        cases['array-null']=([[np.ones((2,2))],[None]],None)
        cases['dict-subclass']=([[OrderedDict([(np.int64(7),{'inside':1})])]],True)
        cases['np-datetime']=([[np.datetime64('2024-01-01T00:00:00.123456789')]],None)
    except ImportError:pass
    try:
        import tensorflow as tf
        cases.update({'tf-scalar':([[tf.constant(2.5)]],True),'tf-vector':([[tf.constant([1.,2.])]],True),'tf-long':([[tf.range(40)]],True)})
    except ImportError:pass
    for name,(data,mixed) in cases.items():
        artifact=w.Artifact('case','dataset');table=w.Table(columns=[0 if name=='integer_column' else 'x'],data=data,allow_mixed_types=bool(mixed))
        def serialize():
            value=table.to_json(run_or_artifact=artifact)
            out['cases'][name]={'json':simple(value),'schema_after':simple(table._column_types.to_json(artifact)),
                                'mapping_key_types':[type(key).__name__ for key in value['data'][0][0]] if name=='dict-subclass' else None,
                                'decoded':outcome(lambda: {'rows':w.Table.from_json(value,Source(artifact)).data,'schema':w.Table.from_json(value,Source(artifact))._column_types.to_json(artifact)})}
        captured=outcome(serialize)
        if 'error' in captured:out['cases'][name]=captured
    # Actual file binding and explicit run serialization. Only publication and
    # telemetry records are replaced, leaving the file/descriptor code intact.
    boundary=patch.object(w,'Run',ProbeRun) if reference else patch.object(importlib.import_module('backprop.run'),'Run',ProbeRun)
    with boundary:
        out['runs']=[]
        for mode in ['IMMUTABLE','MUTABLE','INCREMENTAL']:
            run=ProbeRun(root/mode);table=w.Table(columns=['x'],data=[[1]],log_mode=mode)
            row={'mode':mode,'unbound':outcome(lambda:table.to_json(run))}
            table.bind_to_run(run,'table',0,id_='fixed');row['bound']=simple(table.to_json(run_or_artifact=run))
            row['wrong']=outcome(lambda:table.to_json(ProbeRun(root/'other')))
            artifact=w.Artifact('bound','dataset');row['artifact']=simple(table.to_json(artifact));out['runs'].append(row)
    custom_owner=w.Artifact('custom','dataset');custom=w.Table(columns=['value'],data=[[5]],dtype=ProbeType(3))
    custom_json=custom.to_json(custom_owner);rebuilt=w.Table.from_json(custom_json,Source(custom_owner))
    out['custom_type']={'descriptor':simple(custom_json),'decoded':simple(rebuilt.data),
        'rejected':outcome(lambda:rebuilt.add_data(2)),'accepted':outcome(lambda:rebuilt.add_data(8)),'after':simple(rebuilt.data)}
    out['logged']=[]
    with w.init(project='public-table-reference',dir=str(root/'logs'),mode='offline',settings={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True}) as run:
        table=w.Table(columns=['x'],log_mode='INCREMENTAL')
        for value in range(2):
            table.add_data(value);run.log({'values':table});descriptor=table.to_json(w.Artifact('explicit','dataset'))
            out['logged'].append({'data':descriptor['data'],'nrows':descriptor['nrows'],'increment_num':descriptor['increment_num'],'prior_files':len(descriptor['previous_increments_paths'])})
    out['relationships']={} 
    for mode in ['key','index']:
        artifact=w.Artifact('relations','dataset');base=w.Table(columns=['key','v'],data=[['a',1],['b',2]]);base.set_pk('key')
        table=w.Table(columns=['ref'],data=[['b']] if mode=='key' else [[base.index_ref(1)]])
        if mode=='key':table.set_fk('ref',base,'key')
        descriptor=table.to_json(artifact);restored=w.Table.from_json(descriptor,Source(artifact))
        out['relationships'][mode]={'json':simple(descriptor),'data':simple(restored.data),'target_rows':simple(restored.data[0][0]._table.data)}
    out['increments']=[]
    for mode in ['valid','missing_number','invalid_json','missing_data','bad_name']:
        artifact=w.Artifact('increments','dataset');table=w.Table(columns=['x'],data=[[1],[2]],log_mode='INCREMENTAL');descriptor=table.to_json(artifact)
        for name,data in [('1-200.value.table.json',[[2]]),('0-100.value.table.json',[[1]])]:
            with artifact.new_file(name,mode='w') as stream:json.dump({**descriptor,'data':data},stream)
        if mode=='missing_number':descriptor.pop('increment_num')
        if mode in ['invalid_json','missing_data','bad_name']:
            with artifact.new_file('bad.table.json',mode='w') as stream:
                stream.write('bad-json' if mode=='invalid_json' else json.dumps({} if mode=='missing_data' else {'data':[[3]]}))
        source=Source(artifact)
        out['increments'].append({'mode':mode,'result':outcome(lambda:w.Table.from_json(descriptor,source).data)})
    out['repeated']=[]
    for mode in ['IMMUTABLE','MUTABLE','INCREMENTAL']:
        table=w.Table(columns=['x'],data=[[1],[2]],log_mode=mode);artifact=w.Artifact('repeat','dataset')
        # These are the cursor attributes used by each SDK's durable log path.
        if reference:table._last_logged_idx=0
        else:table._offset=1
        first=table.to_json(artifact);second=table.to_json(artifact)
        out['repeated'].append({'mode':mode,'first':simple(first),'second':simple(second)})
    out['strict']=[]
    for strict,limit in [(False,False),(True,False),(False,True)]:
        table=w.Table(columns=['x'],data=[[1],[2]]);artifact=w.Artifact('strict','dataset')
        run=SimpleNamespace(settings=SimpleNamespace(strict=strict,table_raise_on_max_row_limit_exceeded=limit))
        with patch.object(w.Table,'MAX_ARTIFACT_ROWS',1),patch.object(w,'run',run):
            out['strict'].append(outcome(lambda:table.to_json(artifact)))
    table=w.Table(columns=['x'],data=[[1]])
    out['invalid']['owner']=[outcome(lambda value=value:table.to_json(value)) for value in [None,{},1,object()]]
    out['invalid']['calls']=[outcome(table.to_json),outcome(lambda:table.to_json(artifact=None)),outcome(lambda:w.Table.from_json({'columns':[],'data':[]})),outcome(lambda:w.Table.from_json(data={},source_artifact=None))]
    out['invalid']['data']=[outcome(lambda value=value:w.Table.from_json(value,None)) for value in [None,[],{}, {'columns':['x']},{'columns':['x'],'data':[[1,2]]},{'columns':['x'],'data':[[1]],'column_types':{}},{'columns':['x'],'data':[[1]],'log_mode':'BAD'},{'columns':['x'],'data':[[1]],'log_mode':None}]]
    artifact=w.Artifact('cap','dataset');table=w.Table(columns=['x'],data=[[1],[2]])
    with patch.object(w.Table,'MAX_ARTIFACT_ROWS',1):out['cap']=simple(table.to_json(artifact))
print(json.dumps(out,sort_keys=True,default=str))
