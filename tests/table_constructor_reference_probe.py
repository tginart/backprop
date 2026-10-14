"""Exercise complete constructor input selection with genuine isolated SDKs.

Architecture: real list/NumPy/pandas arguments -> constructor and subsequent
row/cast/public serialization -> observable types, schemas and error timing.
Only EvalTable's external Weave dependency gate is replaced. No SDK constructor,
validation or serializer is replaced. Main caller: test_optional_table_constructor.
"""
from contextlib import ExitStack
import importlib
import inspect
import json
import sys
from unittest.mock import patch
reference=sys.argv[1]=='reference'
if reference:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
try:import numpy as np
except ImportError:np=None
try:import pandas as pd
except ImportError:pd=None
from wandb.sdk.data_types import _dtypes
from wandb.sdk.data_types.table import _PrimaryKeyType, _ForeignIndexType


def simple(value):
    if np is not None and isinstance(value,np.ndarray):return {'array':simple(value.tolist()),'dtype':str(value.dtype),'shape':list(value.shape)}
    if np is not None and isinstance(value,np.generic):return {'scalar':value.item(),'dtype':str(value.dtype)}
    if isinstance(value,dict):return {str(key):simple(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):return [simple(item) for item in value]
    if isinstance(value,(str,int,float,bool)) or value is None:return value
    return {'class':type(value).__name__}


def state(table):
    return {'columns':table.columns,'data':simple(table.data),'schema':table._column_types.to_json(),'mode':table.log_mode,
            'keys':[[isinstance(value, (str,int)) and hasattr(value,'_table') for value in row] for row in table.data]}


def result(call):
    try:return {'value':simple(call())}
    except Exception as error:return {'error':type(error).__name__}


def cases():
    base={'default':{},'empty':{'columns':[]},'empty_rows':{'columns':[],'data':[[],[]]},
        'list_rows':{'columns':['x'],'data':[[1],(2,),iter([3])]},
        'row_strings':{'columns':['a','b'],'data':['ab','cd']},
        'rows':{'columns':['x'],'rows':[[1]]},'data_precedes_rows':{'columns':['x'],'data':[[2]],'rows':[[1]]},
        'empty_data_precedes_rows':{'columns':['x'],'data':[],'rows':[[1]]},
        'tuple_data':{'columns':['x'],'data':((1,),)},'tuple_rows':{'columns':['x'],'rows':((1,),)},
        'dict_data':{'columns':['x'],'data':{'a':1}},'generator_data':{'columns':['x'],'data':iter([[1]])},
        'tuple_columns':{'columns':('x',)},'bool_columns':{'columns':[True]},'integer_columns':{'columns':[0,1],'data':[[1,2]]},
        'mixed_columns':{'columns':['a',1],'data':[[1,2]]},'duplicate_columns':{'columns':['x','x'],'data':[['untyped',1],['raw',2]]},
        'invalid_dataframe':{'dataframe':{'x':[1]}},
        'short_dtype_empty':{'columns':['a','b'],'dtype':[int]},'short_dtype_rows':{'columns':['a','b'],'dtype':[int],'data':[[1,2]]},
        'empty_dtype':{'columns':['a'],'dtype':[]},'long_dtype':{'columns':['a'],'dtype':[int,str],'data':[[1]]},
        'short_optional_empty':{'columns':['a','b'],'optional':[True]},'short_optional_rows':{'columns':['a','b'],'optional':[True],'data':[[1,2]]},
        'empty_optional':{'columns':['a'],'optional':[]},'long_optional':{'columns':['a'],'optional':[True,False],'data':[[None]]},
        'optional_none':{'columns':['a'],'optional':None,'data':[[None]]},'optional_truthy':{'columns':['a'],'optional':'yes','data':[[None]]},
        'optional_tuple':{'columns':['a'],'optional':(False,),'data':[[None]]},
        'duplicate_types':{'columns':['x','x'],'dtype':[int,str],'data':[[1,'a']]},
        'mixed_ignores_dtype':{'columns':['a','b'],'dtype':[],'allow_mixed_types':True,'data':[[1,'a'],['b',2]]},
        'mixed_still_respects_optional':{'columns':['a','b'],'dtype':[],'allow_mixed_types':True,'optional':[True],'data':[[1,'a']]},
        'dtype_tuple_constant':{'columns':['a'],'dtype':(1,2),'data':[[1]]},
        'optional_columns':{'columns':['a','b'],'dtype':[int,str],'optional':[False,True],'data':[[1,None]]},
        'pk':{'columns':['key'],'dtype':_PrimaryKeyType,'optional':False,'data':[['a']]},
        'duplicate_pk_second':{'columns':['key','key'],'dtype':[str,_PrimaryKeyType],'optional':False,'data':[['a','b']]},
        'duplicate_pk_twice':{'columns':['key','key'],'dtype':_PrimaryKeyType,'optional':False},
        'duplicate_pk_removed':{'columns':['key','key'],'dtype':[_PrimaryKeyType,str],'optional':False}}
    if np is not None:
        base.update({'numpy':{'columns':['a','b'],'data':np.array([[1,2]],dtype='i2')},
            'numpy_empty':{'columns':['a'],'data':np.empty((0,1))},'numpy_1d':{'columns':['a'],'data':np.array([1,2])},
            'numpy_3d':{'columns':['a'],'data':np.arange(6).reshape(2,1,3)},'numpy_0d':{'columns':['a'],'data':np.array(1)},
            'numpy_rows':{'columns':['a'],'rows':np.array([[1]])},'numpy_columns':{'columns':np.array(['a'])},
            'numpy_optional':{'columns':['a','b'],'optional':np.array([True,False])},
            'numpy_scalar_optional':{'columns':['a'],'optional':np.bool_(False),'data':[[1]]}})
    if pd is not None:
        frame=pd.DataFrame({'integer':np.array([1,2],dtype='i2'),'float':np.array([1.,2.],dtype='f4')})
        duplicate=pd.DataFrame([[1,2]],columns=['x','x'])
        base.update({'dataframe':{'dataframe':frame},'data_frame':{'data':frame},
            'dataframe_precedence':{'dataframe':frame,'data':[['bad']],'columns':['ignored'],'rows':[['bad']]},
            'dataframe_duplicate':{'dataframe':duplicate},'dataframe_optional':{'dataframe':frame,'optional':False},
            'dataframe_empty':{'dataframe':frame.iloc[:0]},'dataframe_short_dtype':{'dataframe':frame.iloc[:0],'dtype':[int]}})
    return base


out={'constructors':{},'aliases':{},'repairs':{},'modes':{},'column_operations':{}}
with ExitStack() as stack:
    if reference:
        import wandb.integration.weave as integration
        stack.enter_context(patch.object(integration,'ensure_version'))
    for clsname in ('Table','EvalTable'):
        cls=getattr(w,clsname)
        out['constructors'][clsname]={name:result(lambda args=args:state(cls(**args))) for name,args in cases().items()}
        for mode in ['IMMUTABLE','MUTABLE','INCREMENTAL',None,'invalid']:
            out['modes'][clsname+':'+str(mode)]=result(lambda mode=mode:state(cls(columns=['x'],data=[[1]],log_mode=mode)))
        for module in ['wandb','wandb.data_types','wandb.sdk.data_types.'+('table' if clsname=='Table' else 'eval_table')]:
            target=getattr(importlib.import_module(module),clsname);columns=['x']
            table=target(columns=columns,data=[[1]],dtype=int,optional=False)
            same=table.columns is columns; table.add_column('y',[2])
            out['aliases'][module+'.'+clsname]={'same_class':target is cls,'same_columns':same,'input_columns_after_add':columns,'state':state(table),
                'signature':[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(target).parameters.values()]}
        column_cases={'list':('new',[3,4],False),'tuple':('new',(3,4),False),
            'generator':('new',iter([3,4]),False),'wrong_length':('new',[3],False),
            'existing':('x',[3,4],False),'integer':(1,[3,4],False),'optional':('new',[3,4],1)}
        if np is not None:column_cases['array']=('new',np.array([3,4],dtype='i2'),False)
        for name,args in column_cases.items():
            def add(args=args):
                table=cls(columns=['x'],data=[[1],[2]]);table.add_column(*args);return state(table)
            out['column_operations'][clsname+':add:'+name]=result(add)
        for name,convert in [('x',None),('missing',None),('x','invalid'),('x',[]),('x','numpy')]:
            if convert=='numpy' and np is None:continue
            out['column_operations'][clsname+':get:'+str(name)+':'+str(convert)]=result(lambda name=name,convert=convert:cls(columns=['x'],data=[[1],[2]]).get_column(name,convert))
        if np is not None:
            class Duck:
                calls=0
                def to_data_array(self):self.calls+=1;return [42]
            duck=Duck();converted=cls(columns=['object'],data=[[duck]],allow_mixed_types=True).get_column('object','numpy')
            out['column_operations'][clsname+':duck']={'value':simple(converted),'calls':duck.calls}
        source=w.Table(columns=['key'],data=[['a']]);source.set_pk('key')
        for duplicate in (False,True):
            for reference_value in [source.get_index()[0],source.get_column('key')[0]]:
                def linked():
                    values=[reference_value,reference_value] if duplicate else [reference_value]
                    table=cls(columns=['ref','ref'] if duplicate else ['ref'],dtype=[],data=[values])
                    return {'rows':simple(table.data),'types':[type(value).__name__.lstrip('_') for value in table.data[0]],
                        'target':table.data[0][0]._table is source,
                        'declared':list(table._column_types.params['type_map']),
                        'schema_type':table._column_types.params['type_map']['ref'].name}
                out['repairs'][clsname+':reference:'+str(duplicate)+':'+type(reference_value).__name__.lstrip('_')]=result(linked)
        class Derived(cls):
            def cast(self,name,dtype,optional=False):
                calls.append([name,bool(optional)])
                return super().cast(name,dtype,optional)
        calls=[];derived=Derived(columns=['x','x'],dtype=[int,str],optional=[False,True],data=[[1,'a']])
        out['repairs'][clsname+':subclass_cast']={'calls':calls,'state':state(derived)}
        for setting in [{'dtype':[]},{'dtype':[int]},{'optional':[]}]:
            def repair(setting=setting):
                table=cls(columns=['a','b'],**setting);before=state(table)
                rejected=result(lambda:table.add_data(1,2))
                table.cast('a',int);table.cast('b',int);table.add_data(1,2)
                return {'before':before,'rejected':rejected,'after':state(table)}
            out['repairs'][clsname+':'+repr(setting)]=result(repair)
print(json.dumps(out,sort_keys=True))
