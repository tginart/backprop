"""Compare public legacy chart values using genuine pinned W&B processes.

Architecture: verified upstream source -> public constructors/import aliases ->
identity, mutation and dataclass observations. Main callers: SDK tests and CI.
No chart renderer, network response or W&B storage service is mocked here.
"""
import copy
import dataclasses
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
viz=importlib.import_module('wandb.plot.viz')
if REFERENCE:
    assert hashlib.sha256(Path(inspect.getfile(viz.Visualize)).read_bytes()).hexdigest()=='9e2d4c4e4b779354f14aa02b74847fb15703d5c5748fd78a989757d1094170e3'


def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]


def attempt(call):
    try: return {'value':call()}
    except Exception as error: return {'error':("Error" if type(error) is w.Error else type(error).__name__),"public_error_identity":type(error) is w.Error,'message':str(error)}


def spec_values(cls):
    """Exercise both fields, derived properties and ordinary dataclass behavior."""
    cases={}
    for name,args,kwargs in [('default',('id',),{}),('positional',('id','k'),{}),('keywords',(),{'name':'id','key':'k'}),
         ('none',(None,None),{}),('containers',(['id'],{'key':1}),{}),('missing',(),{}),
         ('too_many',('id','k','third'),{}),('extra',('id',),{'extra':True})]:
        def observe():
            value=cls(*args,**kwargs)
            return {'dict':copy.deepcopy(vars(value)),'fields':[field.name for field in dataclasses.fields(value)],
                    'asdict':dataclasses.asdict(value),'repr':repr(value),'match_args':value.__match_args__,
                    'config_key':value.config_key,'config_value':value.config_value,
                    'equality':value==cls(value.name,value.key), 'hash':attempt(lambda:hash(value))}
        cases[name]=attempt(observe)
    value=cls('old','initial');value.name='new';value.key='updated'
    first=value.config_value;first['historyFieldSettings']['key']='external mutation'
    cases['mutation']={'dict':copy.deepcopy(vars(value)),'config_key':value.config_key,'fresh_config':value.config_value,
                       'replace':dataclasses.asdict(dataclasses.replace(value,name='replaced')),
                       'copy':vars(copy.copy(value)), 'deepcopy':vars(copy.deepcopy(value))}
    del value.key
    cases['deleted_key']={'key':value.key,'config':value.config_value}
    del value.name
    cases['deleted_name']=attempt(lambda:value.config_value)
    cases['readonly_property']=attempt(lambda:setattr(cls('id'),'config_key','no'))
    return {'signature':signature(cls),'cases':cases}


def chart_values(cls):
    """The constructor owns no copies or type coercion; set_key delegates."""
    table=w.Table(columns=['x','y'],data=[[0,2]]);spec=viz.VisualizeSpec('wandb/feature_importances/v1')
    cases={}
    for name,args,kwargs in [('positional',(table,spec),{}),('keywords',(),{'table':table,'spec':spec}),
         ('none',(None,None),{}),('raw_fields',('raw table','raw spec'),{}),('duck',(table,SimpleNamespace(name='local',key='k')),{}),
         ('missing',(),{}),('missing_spec',(table,),{}),('too_many',(table,spec,None),{}),('extra',(table,spec),{'extra':True}),
         ('duplicate_table',(table,),{'table':table,'spec':spec})]:
        def observe():
            value=cls(*args,**kwargs)
            return {'type':type(value).__name__,'table_identity':value.table is table,'spec_identity':value.spec is spec,
                    'fields':[field.name for field in dataclasses.fields(value)],'dictionary_keys':list(copy.deepcopy(vars(value))),
                    'match_args':value.__match_args__,'set_key':attempt(lambda:value.set_key('metric'))}
        cases[name]=attempt(observe)
    raw=cls('table','spec')
    cases['dataclass']={'repr':repr(raw),'asdict':dataclasses.asdict(raw),'equality':raw==cls('table','spec'),
        'inequality':raw!=cls('other','spec'),'hash':attempt(lambda:hash(raw)),
        'replace':dataclasses.asdict(dataclasses.replace(raw,table='changed'))}
    value=cls(table,spec)
    for key in ['changed',None,4,['a']]:
        result=value.set_key(key)
        cases['key_'+repr(key)]={'return':result,'key_identity':value.spec.key is key,'config_value':value.spec.config_value}
    other=viz.VisualizeSpec('other');value.spec=other;value.table='new table';result=value.set_key('other key')
    cases['mutation']={'spec_identity':value.spec is other,'table':value.table,'return':result,'key':other.key}
    cases['missing_key']=attempt(lambda:value.set_key())
    cases['extra_key']=attempt(lambda:value.set_key('a','b'))
    del value.spec
    cases['deleted_spec']=attempt(lambda:value.set_key('a'))
    del value.table
    cases['deleted_table']=attempt(lambda:value.table)
    return {'signature':signature(cls),'set_key_signature':signature(cls.set_key),'cases':cases}


def main():
    result={'spec':spec_values(viz.VisualizeSpec),'charts':{},'factories':{}}
    for module in ['wandb.plot','wandb.plot.viz']:
        cls=importlib.import_module(module).Visualize
        result['charts'][module]=chart_values(cls)
        result['charts'][module]['alias_identity']=cls is viz.Visualize
    for module in ['wandb','wandb.plot','wandb.plot.viz']:
        factory=importlib.import_module(module).visualize;table=w.Table(columns=['x'],data=[[1]])
        value=factory('not-yet-registered',table)
        result['factories'][module]={'signature':signature(factory),'type':type(value).__name__,
            'identity':value.table is table,'name':value.spec.name,'key':value.spec.key,
            'invalid':attempt(lambda:factory('id',None))}
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
