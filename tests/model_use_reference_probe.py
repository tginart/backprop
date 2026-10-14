"""Compare complete model-helper control flow with the genuine pinned method.

Architecture: genuine Run construction -> hosted artifact lookup/download
boundary fixtures -> unchanged public helper/decorators -> observations.
Main caller: model-use contract tests. Filesystem inspection is real; this
matrix certifies the helper, not every delegated artifact-path/parser contract.
"""
import hashlib,importlib,inspect,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def attempt(call,root):
    try:
        value=call()
        return {'value':str(value).replace(str(root),'<download>') if value is not None else None,
                'type':type(value).__name__}
    except Exception as error:
        kind='Error' if type(error) is w.Error else type(error).__name__
        return {'error':kind,'message':str(error).replace(str(root),'<download>')}


def make_run(cls,mode,directory):
    settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}
    if REFERENCE:
        run=cls(settings=w.Settings(mode=mode,run_id='model-reader',entity='e',project='models',**settings))
        importlib.import_module('wandb.sdk.wandb_init')._set_global_run(run)
        return run
    return w.init(id='model-reader',entity='e',project='models',mode=mode,dir=directory,
        _sink=w.MemorySink() if mode=='online' else None,_deployment={},settings={**settings,'flush_interval':600})


def finish(run):
    if REFERENCE:
        run._is_finished=True
        importlib.import_module('wandb.sdk.lib.module').unset_globals()
    else:run.finish()


def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default))
            for p in inspect.signature(call).parameters.values()]


def cases():
    return {
        'single_file':{},'empty':{'files':[]},'two_files':{'files':['a.bin','b.bin']},
        'single_directory':{'files':['weights/a.bin','weights/b.bin']},
        'two_directories':{'files':['first/a.bin','second/a.bin']},
        'file_and_directory':{'files':['a.bin','nested/b.bin']},
        'uppercase_type':{'type':'MODEL'},'mixed_type':{'type':'PretrainedModelWeights'},
        'substring_type':{'type':'remodeling'},'wrong_type':{'type':'dataset'},
        'empty_type':{'type':''},'none_type':{'type':None},'numeric_type':{'type':7},
        'keyword':{'kwargs':{'name':'weights:best'},'args':[]},
        'qualified':{'args':['e/models/weights:best']},'project_name':{'args':['models/weights:best']},
        'version_name':{'args':['weights:v3']},'none_name':{'args':[None]},
        'numeric_name':{'args':[7]},'missing_argument':{'args':[]},
        'extra_argument':{'args':['weights:best','extra']},
        'unknown_keyword':{'kwargs':{'unknown':True}},
        'duplicate_argument':{'kwargs':{'name':'other'}},
        'lookup_error':{'lookup_error':True},'download_error':{'download_error':True},
        'missing_download_directory':{'missing_directory':True},
        'offline':{'mode':'offline'},'offline_wrong_type':{'mode':'offline','type':'data'},
        'offline_missing_argument':{'mode':'offline','args':[]},
        'finished':{'finished':True},'finished_missing_argument':{'finished':True,'args':[]},
        'finished_unknown_argument':{'finished':True,'kwargs':{'unknown':True}},
        'finished_offline':{'mode':'offline','finished':True},
    }


out={'imports':{},'disabled':{},'preinit':{}}
# The real disabled-run construction exposes an upstream omission: W&B leaves
# its root model export unbound. Native stable exports deliberately work here.
with tempfile.TemporaryDirectory() as temporary:
    run=w.init(mode='disabled',dir=temporary,settings={'silent':True,'console':'off','x_disable_stats':True})
    out['disabled']['method']=attempt(lambda:run.use_model('missing'),Path(temporary))
    out['disabled']['missing_argument']=attempt(lambda:run.use_model(),Path(temporary))
    out['disabled']['unknown_argument']=attempt(lambda:run.use_model(unknown=True),Path(temporary))
    out['disabled']['global']=attempt(lambda:w.use_model('missing'),Path(temporary))
    run.finish()
for label,args,kwargs in [('name',['missing'],{}),('missing',[],{}),('unknown',[],{'unknown':True})]:
    out['preinit'][label]=attempt(lambda:w.use_model(*args,**kwargs),Path('/irrelevant'))
for label in ['wandb.Run','wandb.sdk.wandb_run.Run','wandb.use_model']:
    module,member=label.rsplit('.',1)
    cls=getattr(importlib.import_module(module),member) if member=='Run' else w.Run
    group={}
    for key,case in cases().items():
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
            root=Path(temporary)/'download';root.mkdir()
            for name in case.get('files',['weights.bin']):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'weights')
            run=make_run(cls,case.get('mode','online'),temporary);calls=[]
            # Only hosted artifact retrieval/download is replaced. The actual
            # decorated helper and os.listdir/path assembly execute unchanged.
            def download():
                calls.append({'download':True})
                if case.get('download_error'):raise OSError('download interrupted')
                return str(root/'absent') if case.get('missing_directory') else str(root)
            def lookup(*args,**kwargs):
                calls.append({'args':args,'kwargs':kwargs})
                if case.get('lookup_error'):raise w.CommError('artifact not visible')
                return SimpleNamespace(type=case.get('type','model'),download=download)
            with patch.object(run,'use_artifact',side_effect=lookup):
                group.setdefault('signature',signature(w.use_model if member=='use_model' else cls.use_model))
                if case.get('finished'):finish(run)
                call=w.use_model if member=='use_model' else run.use_model
                group[key]=attempt(lambda:call(*case.get('args',['weights:best']),**case.get('kwargs',{})),root)
                group[key]['calls']=calls
            if not case.get('finished'):finish(run)
    out['imports'][label]=group

if REFERENCE:
    out['source_hashes']={}
    for name in ['wandb.sdk.wandb_run','wandb.sdk.wandb_init','wandb.sdk.lib.noop_run','wandb.sdk.lib.module']:
        actual=Path(importlib.import_module(name).__file__);expected=ROOT/'.reference/wandb/source'/Path(*name.split('.')).with_suffix('.py')
        assert actual.read_bytes()==expected.read_bytes()
        out['source_hashes'][name]=hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
