"""Compare actual pinned Table binding and JoinedTable reference operations.

Architecture: isolated SDK imports -> real temporary Table files -> recording
publication/artifact boundaries -> signatures, data, errors and lifecycle.
Service fixtures only replace run publication and artifact ownership. Native
persistence and cross-artifact media copies are tested separately.
Main caller: test_optional_table_files. No network or installed-package changes.
"""
import base64
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

reference=sys.argv[1]=='reference'
if reference:
    import wandb as w
    assert w.__version__=='0.30.0'
    from wandb.sdk.data_types.base_types.media import Media
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
    from backprop.media_base import Media


def outcome(call):
    try:return {'value':call()}
    except Exception as error:return {'error':type(error).__name__}


def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]


class ProbeRun:
    def __init__(self,directory,link=False,symlink=False):
        self.dir=str(directory);self.published=[];self._settings=SimpleNamespace(allow_media_symlink=link,symlink=symlink)
    def _publish_file(self,path):self.published.append(path)


class ProbeArtifact:
    def __init__(self):
        self.manifest=self._manifest=SimpleNamespace(entries={});self.calls=[]
    def add(self,value,name):
        path=value.with_suffix(name);self.manifest.entries[path]=None;self.calls.append(['add',type(value).__name__,name]);return SimpleNamespace(path=path)
    def add_reference(self,uri,name):
        self.calls.append(['reference',uri,name]);self.manifest.entries[name]=None;return [SimpleNamespace(path=name)]


out={'aliases':{},'binding':[],'joins':[]}
if reference:out['source_sha256']=hashlib.sha256(Path(inspect.getfile(w.Table)).read_bytes()).hexdigest()
with tempfile.TemporaryDirectory() as directory:
    directory=Path(directory)
    run=ProbeRun(directory/'main')
    boundary=patch.object(w,'Run',ProbeRun) if reference else patch.object(importlib.import_module('backprop.run'),'Run',ProbeRun)
    with boundary:
        for module in ['wandb','wandb.data_types','wandb.sdk.data_types.table']:
            for name in ['Table','JoinedTable']:
                cls=getattr(importlib.import_module(module),name);table=w.Table(columns=['id'],data=[[1]])
                value=cls(columns=['id'],data=[[1]]) if name=='Table' else cls(table,table,'id')
                row={'signature':{key:signature(getattr(cls,key)) for key in ['bind_to_run','file_is_set','is_bound','path_is_reference']},'before':[value.file_is_set(),value.is_bound()]}
                row['result']=outcome(lambda:value.bind_to_run(run,'alias',0,id_=module.replace('.','_')))
                row['after']=[value.file_is_set(),value.is_bound()]
                row['paths']=[outcome(lambda p=p:cls.path_is_reference(p)) for p in [None,Path('s3://x'),'s3://x','gs://x','https://x','ftp://x',{},1,b'https://x']]
                if name=='Table':row['bytes']=Path(value._path).read_text()
                else:
                    artifact=ProbeArtifact();row['json']=value.to_json(artifact);row['calls']=artifact.calls
                    row=json.loads(json.dumps(row).replace(str(id(value)),'IDENTITY'))
                out['aliases'][module+'.'+name]=row
        for mode in ['IMMUTABLE','MUTABLE','INCREMENTAL']:
            for identity,ignore,link,symlink in [(None,None,False,False),(0,False,False,False),('custom',True,False,False),('hard',False,True,False),('soft',True,True,True)]:
                value=w.Table(columns=['x','nested'],data=[[1,{'x':(True,None)}],[2,{'x':(False,None)}]],log_mode=mode,allow_mixed_types=True);value.MAX_ROWS=1
                owner=ProbeRun(directory/(mode+str(identity)),link,symlink)
                row={'mode':mode,'before':[value.file_is_set(),value.is_bound()]}
                row['result']=value.bind_to_run(owner,'/../nested/key','step',id_=identity,ignore_copy_err=ignore)
                row.update(file=Media.to_json(value,owner),bytes=Path(value._path).read_text(),published=list(owner.published),after=[value.file_is_set(),value.is_bound()])
                # Model the durable prior logging cursor in each implementation.
                if reference:value._last_logged_idx=0
                else:value._offset=1
                value.add_data(3,{'x':(True,None)})
                value.bind_to_run(owner,'second',2,id_=identity,ignore_copy_err=ignore);row['second']=Path(value._path).read_text()
                other=ProbeRun(directory/(mode+str(identity)+'-other'));value.bind_to_run(other,'third',3);row['cross']=Path(value._path).read_text()
                row['wrong_owner']=outcome(lambda:Media.to_json(value,owner));out['binding'].append(row)
        limited=w.Table(columns=['x'],data=[[1],[2]])
        with patch.object(w.Table,'MAX_ROWS',1):
            limited.bind_to_run(run,'limit',0)
        out['limited']=Path(limited._path).read_text()
        out['invalid_binding']=[]
        for args,kwargs in [((),{}),((None,'k',0),{}),((run,'k',0),{'unknown':True}),((run,'k'),{}),((run,None,None),{'id_':False})]:
            value=w.Table(columns=['x'],data=[[1]])
            out['invalid_binding'].append({'result':outcome(lambda:value.bind_to_run(*args,**kwargs)),'file':value.file_is_set(),'bound':value.is_bound()})
        for kind in ['table','source_table','partition','entry','path','missing_path']:
            left=w.Table(columns=['id'],data=[[1]])
            if kind=='source_table':
                if reference:left._artifact_source=SimpleNamespace(name='nested/source.table.json')
                else:left._artifact_source_name='nested/source.table.json'
            if kind=='partition':left=importlib.import_module('wandb.sdk.data_types.table').PartitionedTable('parts')
            if kind=='entry':left=SimpleNamespace(ref_url=lambda:'backprop-artifact://abc/source.table.json',digest=base64.b64encode(bytes(range(16))).decode(),name='source.table.json')
            if kind in ['path','missing_path']:left='existing.table.json'
            value=w.JoinedTable(left,w.Table(columns=['id'],data=[[1]]),'id');artifact=ProbeArtifact()
            if kind=='path':artifact.manifest.entries['existing.table.json']=None
            row={'kind':kind,'result':outcome(lambda:value.to_json(artifact)),'calls':artifact.calls,'file':value.file_is_set(),'bound':value.is_bound(),
                 'bind_errors':[outcome(lambda a=a,k=k:value.bind_to_run(*a,**k)) for a,k in [((),{}),((run,),{'nonsense':1}),((None,None,None),{})]],
                 'unlogged_run':outcome(lambda:value.to_json(run)),'invalid_owner':outcome(lambda:value.to_json(None))}
            out['joins'].append(json.loads(json.dumps(row).replace(str(id(value)),'IDENTITY')))
        import datetime
        cases=[[],[[datetime.date(2024,1,1)]],[[datetime.datetime(2024,1,1,12,0)]],[[b'abc']],[[w.Html('<p>value</p>')]],[[w.Table(columns=['x'],data=[[1]])]],[[{1:'x'}]]]
        try:
            import numpy as np
            cases += [[[np.arange(40)]],[[np.array(1)]],[[np.float32(.5)]]]
        except ImportError:pass
        out['data']=[]
        for data in cases:
            def capture(data=data):
                value=w.Table(columns=['x'],data=data,allow_mixed_types=True);value.bind_to_run(run,'data',0);return Path(value._path).read_text()
            out['data'].append(outcome(capture))
print(json.dumps(out,sort_keys=True))
