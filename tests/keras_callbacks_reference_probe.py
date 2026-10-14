"""Compare actual Keras callbacks with pinned W&B in isolated processes.

Architecture: real TensorFlow callbacks/models -> unchanged public methods ->
local artifact encoding and recorded service calls -> portable observations.
Main caller: test_optional_keras_contracts. Only W&B upload/wait/read and internal
usage telemetry are replaced; callback bodies and Keras save behavior execute.
"""
import contextlib
import copy
import hashlib
import importlib
import inspect
import io
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tensorflow as tf

REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
importlib.import_module('wandb.integration.keras')
ROOT=Path(__file__).resolve().parents[1]
DIRECTORY=''

def portable(value):
    """Keep public values while replacing only temporary paths and scalars."""
    if isinstance(value,str):return value.replace(DIRECTORY,'<TMP>') if DIRECTORY else value
    if isinstance(value,dict):return {str(k):portable(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [portable(v) for v in value]
    if isinstance(value,np.generic):return portable(value.item())
    if isinstance(value,float) and not math.isfinite(value):return str(value)
    return value

def attempt(call):
    try:return {'value':portable(call())}
    except Exception as error:
        return {'error':'Error' if type(error) is w.Error else type(error).__name__,
                'public_error_identity':type(error) is w.Error,'message':portable(str(error))}

def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]

def model():
    tf.keras.backend.clear_session();tf.keras.utils.set_random_seed(7)
    value=tf.keras.Sequential([tf.keras.layers.Input(shape=(2,)),tf.keras.layers.Dense(1,name='prediction')],name='contract_model')
    value.compile(optimizer=tf.keras.optimizers.SGD(learning_rate=.125),loss='mse')
    return value

class Recorder:
    """Use actual local artifacts; supply deterministic upload and read replies.

    Main callers: each callback case. Real Artifact.add validates and encodes
    tables/files. The read fixture returns the uploaded scalar table, since the
    real service cannot be used for a portable comparison in CI.
    """
    def __init__(self):self.calls=[];self.tables={};self.fail=None
    def record(self,kind,*args,**kwargs):self.calls.append([kind,portable(args),portable(kwargs)])
    def logged(self,artifact,aliases=None,**kwargs):
        self.record('log_artifact',artifact.name,artifact.type,aliases=aliases,
            files=sorted(artifact.manifest.entries),**kwargs)
        if self.fail:raise self.fail
        return artifact
    def used(self,artifact,**kwargs):self.record('use_artifact',artifact.name,artifact.type,**kwargs);return artifact
    def __enter__(self):
        self.stack=contextlib.ExitStack();self.stack.__enter__()
        self.run=SimpleNamespace(id='keras-contract',log_artifact=self.logged,use_artifact=self.used)
        for name,replacement in [('run',self.run),('log',lambda *a,**k:self.record('log',*a,**k)),
                ('define_metric',lambda *a,**k:self.record('define_metric',*a,**k)),
                ('termwarn',lambda *a,**k:self.record('warning',*a,**k)),
                ('termerror',lambda *a,**k:self.record('error',*a,**k)),('log_artifact',self.logged)]:
            self.stack.enter_context(patch.object(w,name,replacement))
        original=w.Artifact.add
        def add(artifact,value,name,*args,**kwargs):
            entry=original(artifact,value,name,*args,**kwargs)
            self.tables[id(artifact),name]=value
            self.record('add_table',name,columns=value.columns,data=value.data)
            return entry
        def get(artifact,name):
            value=self.tables[id(artifact),name]
            self.record('get_table',name)
            return w.Table(columns=list(value.columns),data=copy.deepcopy(value.data))
        self.stack.enter_context(patch.object(w.Artifact,'add',add))
        self.stack.enter_context(patch.object(w.Artifact,'wait',lambda artifact,*a,**k:artifact))
        self.stack.enter_context(patch.object(w.Artifact,'get',get))
        if REFERENCE:
            telemetry=importlib.import_module('wandb.sdk.lib.telemetry')
            self.stack.enter_context(patch.object(telemetry,'context',lambda **kw:contextlib.nullcontext(SimpleNamespace(feature=SimpleNamespace()))))
        return self
    def __exit__(self,*args):return self.stack.__exit__(*args)

def metrics_cases(cls):
    """Exercise constructor switches, every hook, model state and log errors."""
    cases={}
    for label,args,kwargs in [('defaults',(),{}),('batch',('batch',),{}),('every_two',(2,10),{}),
            ('epoch',(),{'log_freq':'epoch','initial_global_step':5}),('zero',(0,),{}),('negative',(-2,),{}),
            ('boolean',(True,),{}),('bad_initial_step',('batch',None),{}),('float_initial_step',('batch',2.5),{}),('unknown',('unknown',),{}),('none',(None,),{}),('extra',(),{'extra':True}),('positional_extra',('epoch',0,1),{})]:
        with Recorder() as recorder:
            def scenario():
                callback=cls(*args,**kwargs);callback.set_model(model());out={}
                for method,arguments in [('on_batch_end',(0,{'loss':2})),('on_train_batch_end',(1,{'loss':1})),('on_train_batch_end',(2,{})),('on_epoch_end',(0,{'loss':.5}))]:
                    out.setdefault(method,[]).append(attempt(lambda:getattr(callback,method)(*arguments)))
                out['state']=[callback.logging_batch_wise,callback.log_freq,callback.global_batch,callback.global_step]
                return out
            cases[label]={'result':attempt(scenario),'calls':recorder.calls}
    for method in ['on_batch_end','on_train_batch_end','on_epoch_end']:
        for label,logs in [('none',None),('empty',{}),('raw',{'name':'label','flag':False}),('invalid',['bad'])]:
            with Recorder() as recorder:
                callback=cls(log_freq='batch');callback.set_model(model())
                cases[method+'_'+label]={'result':attempt(lambda:getattr(callback,method)(0,logs=logs)),'calls':recorder.calls,'step':callback.global_step}
        with Recorder():
            callback=cls();cases[method+'_no_model']=attempt(lambda:getattr(callback,method)(0,{}))
            cases[method+'_missing']=attempt(lambda:getattr(callback,method)())
            cases[method+'_extra']=attempt(lambda:getattr(callback,method)(0,{},extra=True))
    with Recorder() as recorder:
        callback=cls('batch');callback.set_model(SimpleNamespace(optimizer=SimpleNamespace(learning_rate=lambda **kw:tf.constant(.25))))
        cases['callable_lr']={'result':attempt(lambda:callback.on_batch_end(0)), 'calls':recorder.calls}
    with Recorder() as recorder:
        callback=cls();callback.set_model(SimpleNamespace(optimizer=SimpleNamespace(learning_rate=object())))
        cases['invalid_lr']={'result':attempt(lambda:callback.on_epoch_end(0)), 'calls':recorder.calls}
    with Recorder():
        w.run=None;cases['no_run']=attempt(cls)
    return cases

def evaluation_cases(cls):
    """Exercise abstract methods, hook order, table inputs and artifact options."""
    class Evaluation(cls):
        def add_ground_truth(self,logs=None):self.data_table.add_data(0,'sample')
        def add_model_predictions(self,epoch,logs=None):self.pred_table.add_data(epoch,'prediction')
    cases={}
    with Recorder():
        cases['abstract']=attempt(lambda:cls(['id'],['prediction']))
        cases['missing_columns']=attempt(Evaluation)
        cases['extra_constructor']=attempt(lambda:Evaluation(['id'],['prediction'],extra=True))
        callback=Evaluation(['id','input'],['epoch','prediction'])
        cases['abstract_ground_truth']=attempt(lambda:cls.add_ground_truth(callback))
        cases['abstract_prediction']=attempt(lambda:cls.add_model_predictions(callback,0))
        w.run=None;cases['no_run']=attempt(lambda:Evaluation([],[]))
    with Recorder() as recorder:
        data=['id','input'];pred=['epoch','prediction'];callback=Evaluation(data,pred)
        cases['column_identity']=[callback.data_table_columns is data,callback.pred_table_columns is pred]
        cases['hooks']={'begin':attempt(lambda:callback.on_train_begin({'loss':1})),
            'epoch':attempt(lambda:callback.on_epoch_end(3,{'loss':.5})),
            'repeat_begin':attempt(callback.on_train_begin),'repeat_epoch':attempt(lambda:callback.on_epoch_end(4)),
            'calls':recorder.calls}
    for method in ['init_data_table','init_pred_table']:
        for label,columns in [('names',['id','value']),('empty',[]),('tuple',('id','value')),('none',None),('invalid',42),('duplicate',['x','x'])]:
            with Recorder():
                callback=Evaluation([],[])
                def scenario():
                    result=getattr(callback,method)(column_names=columns)
                    table=getattr(callback,'data_table' if method=='init_data_table' else 'pred_table')
                    return {'return':result,'columns':table.columns,'data':table.data}
                cases[method+'_'+label]=attempt(scenario)
    for method in ['log_data_table','log_pred_table']:
        options=[('default',{}),('custom',{'type':'custom','table_name':'results'}),('aliases',{'aliases':['best','reviewed']}),('empty_aliases',{'aliases':[]})] if method=='log_pred_table' else [('default',{}),('custom',{'name':'custom-data','type':'custom','table_name':'results'})]
        for label,kwargs in options:
            with Recorder() as recorder:
                callback=Evaluation(['id','input'],['epoch','prediction']);callback.init_data_table(['id','input']);callback.add_ground_truth()
                callback.init_pred_table(['epoch','prediction']);callback.add_model_predictions(2)
                cases[method+'_'+label]={'result':attempt(lambda:getattr(callback,method)(**kwargs)),'calls':recorder.calls}
        with Recorder():
            callback=Evaluation([],[])
            cases[method+'_uninitialized']=attempt(getattr(callback,method))
            cases[method+'_extra']=attempt(lambda:getattr(callback,method)(extra=1))
            callback.init_data_table([]);callback.init_pred_table([]);w.run=None
            cases[method+'_no_run']=attempt(getattr(callback,method))
    for method in ['on_train_begin','on_epoch_end','add_ground_truth','add_model_predictions','init_data_table','init_pred_table']:
        with Recorder():
            callback=Evaluation([],[])
            cases[method+'_extra']=attempt(lambda:getattr(cls,method)(callback,extra=True))
    return cases

def checkpoint_cases(cls):
    """Use real Keras save decisions and local files for every checkpoint flag."""
    cases={};repairs={}
    options=[('all',{'filepath':'all-{epoch:02d}.keras','monitor':'loss','verbose':0}),
        ('weights',{'filepath':'weights-{epoch:02d}.weights.h5','save_weights_only':True,'monitor':'loss','mode':'min'}),
        ('batch',{'filepath':'batch-{epoch:02d}-{batch:02d}.keras','save_freq':2,'monitor':'loss'}),
        ('path',{'filepath':Path('path.keras'),'monitor':'loss','verbose':1}),
        ('maximum',{'filepath':'max-{epoch:02d}.keras','monitor':'accuracy','mode':'max','initial_value_threshold':.2}),
        ('best',{'filepath':'best-{epoch:02d}.keras','monitor':'loss','save_best_only':True,'mode':'min'}),
        ('threshold',{'filepath':'threshold-{epoch:02d}.keras','monitor':'loss','save_best_only':True,'initial_value_threshold':1.0}),
        ('invalid_freq',{'filepath':'invalid.keras','save_freq':'invalid'}),
        ('invalid_suffix',{'filepath':'invalid.txt'}),('extra',{'filepath':'extra.keras','unknown':True}),
        ('invalid_mode',{'filepath':'mode.keras','mode':'invalid'}),
        ('weights_suffix',{'filepath':'weights.keras','save_weights_only':True}),
        ('forwarded_options',{'filepath':'options-{epoch:02d}.weights.h5','save_weights_only':True,'options':None})]
    for label,kwargs in options:
        folder=Path(DIRECTORY)/label;folder.mkdir(exist_ok=True)
        kwargs={**kwargs,'filepath':folder/kwargs['filepath'] if label=='path' else str(folder/kwargs['filepath'])}
        with Recorder() as recorder:
            def scenario():
                callback=cls(**kwargs);callback.set_model(model());out=[]
                for epoch,loss in enumerate([3.,4.,.5]):
                    callback.on_epoch_begin(epoch)
                    if label=='batch':
                        for batch in range(3):out.append(attempt(lambda:callback.on_train_batch_end(batch,{'loss':loss})))
                    else:out.append(attempt(lambda:callback.on_epoch_end(epoch,{'loss':loss,'accuracy':1/loss})))
                return {'hooks':out,'files':sorted(p.name for p in folder.iterdir()),'all_nonempty':all(p.stat().st_size>0 for p in folder.iterdir()),'old':callback.is_old_tf_keras_version}
            observed={'result':attempt(scenario),'calls':recorder.calls}
            (repairs if label in ['best','threshold','path'] else cases)[label]=observed
    with Recorder():
        cases['missing_path']=attempt(cls);w.run=None
        cases['no_run']=attempt(lambda:cls(str(Path(DIRECTORY)/'no-run.keras')))
    for method in ['on_epoch_end','on_train_batch_end']:
        for label,logs in [('none',None),('empty',{}),('invalid',['bad'])]:
            with Recorder() as recorder:
                callback=cls(str(Path(DIRECTORY)/(method+label+'.keras')),save_freq=1 if method=='on_train_batch_end' else 'epoch')
                callback.set_model(model());callback.on_epoch_begin(0)
                cases[method+'_'+label]={'result':attempt(lambda:getattr(callback,method)(0,logs=logs)),'calls':recorder.calls}
        with Recorder():
            callback=cls(str(Path(DIRECTORY)/'unbound.keras'),save_freq=1)
            cases[method+'_missing']=attempt(lambda:getattr(callback,method)())
            cases[method+'_extra']=attempt(lambda:getattr(callback,method)(0,{},extra=True))
            callback.on_epoch_begin(0)
            cases[method+'_no_model']=attempt(lambda:getattr(callback,method)(0,{'loss':1}))
    with Recorder() as recorder:
        callback=cls(str(Path(DIRECTORY)/'storage.keras'));callback.set_model(model());recorder.fail=ValueError('storage rejected artifact')
        repairs['storage_error']={'result':attempt(lambda:callback.on_epoch_end(0,{'val_loss':1})),'calls':recorder.calls}
    for version in ['2.5.0','2.6.0','3.0.0','bad']:
        with Recorder(),patch.object(tf.keras,'__version__',version,create=True):
            callback=cls(str(Path(DIRECTORY)/'version.keras'))
            cases['version_'+version]=attempt(lambda:callback.is_old_tf_keras_version)
            if version!='bad':
                tf.keras.__version__='9.0.0';cases['cached_'+version]=callback.is_old_tf_keras_version
    return cases,repairs

def main():
    global DIRECTORY
    output={'portable':{},'repairs':{},'source_hashes':{},'versions':{'tensorflow':tf.__version__}}
    with tempfile.TemporaryDirectory() as directory:
        DIRECTORY=directory
        for prefix in ['wandb.keras','wandb.keras.callbacks','wandb.integration.keras','wandb.integration.keras.callbacks']:
            module=w
            for part in prefix.split('.')[1:]:module=getattr(module,part)
            group={};output['portable'][prefix]=group
            # Save fixtures belong to one import case, so aliases cannot inherit
            # an earlier callback's files or appear to have saved stale data.
            for child in Path(directory).iterdir():
                import shutil
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            for name,probe in [('WandbMetricsLogger',metrics_cases),('WandbEvalCallback',evaluation_cases),('WandbModelCheckpoint',checkpoint_cases)]:
                cls=getattr(module,name)
                if REFERENCE:
                    actual=Path(inspect.getfile(cls)).read_bytes();expected=(ROOT/'.reference/wandb/source/wandb/integration/keras/callbacks'/Path(inspect.getfile(cls)).name).read_bytes()
                    assert actual==expected
                    output['source_hashes'][name]=hashlib.sha256(actual).hexdigest()
                methods={key:signature(value.fget if isinstance(value,property) else value) for key,value in vars(cls).items() if not key.startswith('_') and (callable(value) or isinstance(value,property))}
                result=probe(cls)
                if name=='WandbModelCheckpoint':result,output['repairs'][prefix]=result
                group[name]={'signature':signature(cls),'methods':methods,'cases':result}
    print(json.dumps(portable(output),sort_keys=True))

if __name__=='__main__':
    # Keras's verbose checkpoint messages are not part of the JSON channel.
    buffer=io.StringIO()
    with contextlib.redirect_stdout(buffer):main()
    print(buffer.getvalue().splitlines()[-1])
