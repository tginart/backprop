"""Compare the complete Classes value lifecycle in independent processes.

Architecture: pinned/native imports -> ordinary and file-subclass instances ->
constructor, mutation, binding, serialization and error observations. Only the
run service boundary is a fixture; actual file copies and artifact manifests run.
Main caller: test_sdk_classes. Reference files are hash checked before execution.
"""
import contextlib
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

reference=sys.argv[1]=='reference'
if reference:
    import wandb as w
    from wandb.sdk.data_types.helper_types.classes import Classes
    from wandb.sdk.data_types.base_types.media import Media
    assert w.__version__=='0.30.0'
    assert hashlib.sha256(Path(inspect.getfile(Classes)).read_bytes()).hexdigest()=='6db152b6d299b49079a8d571f517aecedbdfa833b8aa475fd6c6a2cc4ff70364'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
    from backprop.media import Classes,Media


def result(call):
    try:return {'value':call()}
    except Exception as error:return {'error':type(error).__name__}


def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default))
            for p in inspect.signature(call).parameters.values()]


class ProbeRun:
    def __init__(self,directory):
        self.dir=str(directory);self.published=[]
        self._settings=SimpleNamespace(allow_media_symlink=False,symlink=False)
    def _publish_file(self,path):self.published.append(path)


class FileClasses(Classes):
    @classmethod
    def get_media_subdir(cls):return 'media/class-files'


output={'aliases':{},'cases':[],'files':[]}
with tempfile.TemporaryDirectory() as directory:
    directory=Path(directory)
    patch_run=patch.object(w,'Run',ProbeRun) if reference else patch.object(importlib.import_module('backprop.run'),'Run',ProbeRun)
    with patch_run:
        for module in ['wandb','wandb.data_types','wandb.sdk.data_types.helper_types.classes']:
            cls=getattr(importlib.import_module(module),'Classes')
            row={'identity':cls is Classes,'is_media':issubclass(cls,Media),'signatures':{}}
            for name in ['__init__','bind_to_run','captions','file_is_set','from_json','get_media_subdir','get_type','init_from_json','is_bound','path_is_reference','to_data_array','to_json','type_mapping','with_suffix']:
                row['signatures'][name]=signature(getattr(cls,name))
            item=cls([{'id':0,'name':'zero'}]);row['json']=item.to_json(None)
            row['file']=item.file_is_set();row['bound']=item.is_bound()
            row['bind']=result(lambda:item.bind_to_run(ProbeRun(directory/'run'),'labels',0,id_='chosen',ignore_copy_err=True))
            row['restore']=cls.from_json(json_obj=row['json'],source_artifact=None).to_json(None)
            output['aliases'][module]=row
        for labels in [[],(),[{'id':0,'name':'zero'}],({'id':'a','name':'A'},),[{'id':0,'name':'zero','color':'red'}],[{'id':0,'name':'a'},{'id':0,'name':'b'}],[{'id':0}],[{'name':'one'}],[None],[3],None,3,{'id':1,'name':'one'}]:
            def construct():
                item=Classes(labels)
                return {'same_sequence':item._class_set is labels,'json':item.to_json(None),'file':item.file_is_set(),'bound':item.is_bound(),'caption':item._caption}
            output['cases'].append(result(construct))
        labels=[{'id':1,'name':'old'}];item=Classes(labels);serialized=item.to_json(None)
        labels[0]['name']='renamed';labels.append({'id':2,'name':'second'})
        output['mutability']={'same':serialized['class_set'] is labels,'json':serialized,'equals':item==Classes(labels),'unequal':item!=Classes([]),'foreign':item==object()}
        artifact=w.Artifact('labels-probe','dataset')
        output['empty_artifact']=item.to_json(artifact)
        output['invalid_owner']=item.to_json(object())
        output['unbound_run']=result(lambda:item.to_json(ProbeRun(directory/'plain')))
        output['missing_json_arg']=result(lambda:item.to_json())
        output['missing_owner_arg']=result(lambda:Classes.from_json({'class_set':labels}))
        output['malformed_json']=[result(lambda value=value:Classes.from_json(value,None).to_json(None)) for value in [{},{'class_set':None},{'class_set':[{}]},None]]
        output['references']=[result(lambda path=path:Classes.path_is_reference(path)) for path in [None,'','plain',Path('a'),'http://a','https://a','s3://a','gs://a','ftp://a','file:///a',1,{},b'https://a']]
        output['captions']=[result(lambda values=values:Classes.captions(values)) for values in [[],[item],[item,item],[object()]]]
        item._caption='label caption';output['caption_json']=item.to_json(None)
        output['caption_artifact']=item.to_json(artifact)
        output['caption_list']=Classes.captions([item,Classes([])])
        output['array']=result(item.to_data_array)
        for temporary,identity in [(False,None),(False,0),(False,'chosen'),(True,'move')]:
            source=directory/f'source-{identity}.json';source.write_text('{"label":1}')
            value=FileClasses(labels);value._set_file(str(source),is_tmp=temporary)
            run=ProbeRun(directory/f'run-{identity}');other=ProbeRun(directory/f'other-{identity}')
            row={'unbound':result(lambda:value.to_json(run)),'return':value.bind_to_run(run,'nested/labels',3,id_=identity),'bound':value.is_bound(),'file':value.file_is_set(),'source_exists':source.exists(),'json':value.to_json(run),'published':run.published.copy()}
            row['wrong_owner']=result(lambda:value.to_json(other))
            row['same_file']=result(lambda:value.bind_to_run(run,'nested/labels',3,id_=identity))
            row['ignore_error']=value.bind_to_run(run,'nested/labels',3,id_=identity,ignore_copy_err=True)
            value.bind_to_run(other,'cross',4,id_=1)
            row['cross_bound']=value.to_json(other);row['old_owner']=result(lambda:value.to_json(run))
            row['artifact']=value.to_json(artifact);row['artifact_repeat']=value.to_json(artifact)
            output['files'].append(row)
print(json.dumps(output,sort_keys=True))
