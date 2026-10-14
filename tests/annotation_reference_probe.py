"""Compare image-annotation contracts in isolated native/reference processes.

Architecture: hash-pinned concrete classes -> validation/input matrices -> real
file binding and artifact serialization. The run service is a recording fixture;
file bytes, image encoders and public artifact methods execute normally.
Main caller: test_optional_annotations. No W&B service or credentials are used.
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
from unittest.mock import patch

import numpy as np
from PIL import Image as PILImage
reference=sys.argv[1]=='reference'
if reference:
    import wandb as w
    from wandb.sdk.data_types.helper_types.bounding_boxes_2d import BoundingBoxes2D
    from wandb.sdk.data_types.helper_types.image_mask import ImageMask
    assert w.__version__=='0.30.0'
    for cls,expected in [(BoundingBoxes2D,'b190e042546af2093a21b9e0dd454b3a7d261bb34f9e2db6d3fe7724e9ab06b0'),(ImageMask,'7598f0d5552fc6dd9f216b51f6bbe6849cca4db88d51334d7f58002194eebd81')]:
        assert hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()==expected
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
    from backprop.media import BoundingBoxes2D,ImageMask


def normalize(value):
    if isinstance(value,dict):return {str(k):normalize(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [normalize(v) for v in value]
    if isinstance(value,np.ndarray):return value.tolist()
    if isinstance(value,np.generic):return value.item()
    return value


def result(call):
    try:return {'value':normalize(call())}
    except Exception as error:return {'error':type(error).__name__}


def signature(call):
    return [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]


class ProbeRun:
    def __init__(self,directory,link=False,symlink=False):
        self.dir=str(directory);self.published=[];self.singletons=[]
        self._settings=SimpleNamespace(allow_media_symlink=link,symlink=symlink)
    def _publish_file(self,path):self.published.append(path)
    def _add_singleton(self,*args):self.singletons.append(normalize(copy.deepcopy(args)))


box={'position':{'minX':.1,'minY':.2,'maxX':.8,'maxY':.9},'class_id':3,'scores':{'confidence':.8},'box_caption':'object'}
center={'position':{'middle':[.5,.5],'width':1,'height':2},'class_id':1}
box_cases=[{'box_data':[]},{'box_data':[box]},{'box_data':[center]},
    {'box_data':[box],'class_labels':{3:'custom'}},{'box_data':[{'position':box['position']}],'class_labels':{}},
    {'box_data':[{'position':box['position']}]},{},None,{'box_data':None},{'box_data':()},
    {'box_data':[{}]},{'box_data':[None]},{'box_data':[{'position':{}}]},
    {'box_data':[dict(center,position={'middle':['x','y'],'width':1,'height':2})]},
    {'box_data':[dict(center,position={'middle':None,'width':1,'height':2})]},
    *[{'box_data':[dict(box,**{field:value})]} for field,value in [('class_id',1.2),('class_id',np.int32(1)),('box_caption',2),('scores',None),('scores',{'bad':'a'}),('scores',{1:.2})]],
    *[{'box_data':[box],'class_labels':labels} for labels in [None,[],{'3':'wrong'},{3:4},{3.5:'float'},{True:'bool'}]],
    {'box_data':[dict(center,position={'middle':np.array([.5,.4]),'width':np.float32(1),'height':2})]}]
mask_cases=[{'mask_data':np.array([[0,1],[127,255]],dtype=np.uint8)},
    {'mask_data':np.array([[.1,1.9],[128.5,255.]])},{'mask_data':np.zeros((2,2),dtype=bool)},
    {'mask_data':np.zeros((2,2)),'class_labels':{0:'background'}},
    {'mask_data':np.zeros((2,2)),'class_labels':{1.5:'float'}},
    {'mask_data':np.zeros((0,2))},{'mask_data':np.array([1,2])},{'mask_data':np.zeros((2,2,1))},
    {'mask_data':np.array([[-1,0]])},{'mask_data':np.array([[256,0]])},{'mask_data':np.array([[float('nan'),0]])},
    {'mask_data':[[0,1]]},{'mask_data':None},{},None,{'path':None},
    *[{'mask_data':np.zeros((1,1)),'class_labels':labels} for labels in [None,[],{'0':'wrong'},{0:3}]]]
output={'aliases':{},'constructors':{},'validators':{},'binding':{},'decoding':{},'helpers':{}}
with tempfile.TemporaryDirectory() as directory:
    directory=Path(directory);image_path=directory/'input.png';PILImage.fromarray(np.array([[0,255]],dtype=np.uint8)).save(image_path)
    mask_cases.extend([{'path':str(image_path)},{'path':image_path},{'path':str(directory/'missing.png')}])
    boundary=patch.object(w,'Run',ProbeRun) if reference else patch.object(importlib.import_module('backprop.run'),'Run',ProbeRun)
    with boundary:
        for cls,cases,suffix in [(BoundingBoxes2D,box_cases,'bounding_boxes_2d'),(ImageMask,mask_cases,'image_mask')]:
            name=cls.__name__;output['constructors'][name]=[];output['validators'][name]=[];output['binding'][name]=[]
            for module_name in ['wandb.data_types','wandb.sdk.data_types.helper_types.'+suffix]:
                alias=getattr(importlib.import_module(module_name),name)
                output['aliases'][module_name]={'same':alias is cls,'signatures':{method:signature(getattr(alias,method)) for method in ['__init__','validate','bind_to_run','to_json','from_json','file_is_set','is_bound','captions','path_is_reference','to_data_array']}}
                sample=alias(copy.deepcopy(cases[0]),'alias')
                alias_run=ProbeRun(directory/(name+'-'+module_name));alias_artifact=w.Artifact('alias-'+name.lower(),'dataset')
                sample.bind_to_run(alias_run,'alias',1,id_='fixed')
                artifact_json=sample.to_json(alias_artifact)
                owner=SimpleNamespace(get_entry=lambda path:SimpleNamespace(download=lambda:sample._path)) if cls is ImageMask else alias_artifact
                output['aliases'][module_name]['calls']={'validation':sample.validate(copy.deepcopy(cases[0])),
                    'file':sample.file_is_set(),'bound':sample.is_bound(),'json':sample.to_json(alias_run),
                    'artifact':artifact_json,'restored_type':alias.from_json(artifact_json,owner).type_name(),
                    'array':result(sample.to_data_array),'caption':alias.captions([sample]),
                    'reference':alias.path_is_reference('s3://bucket/file')}
            def valid():return cls(copy.deepcopy(cases[0]),'labels')
            for case in cases:
                value=copy.deepcopy(case)
                def construct():
                    item=cls(value,'labels')
                    payload=json.loads(Path(item._path).read_text()) if cls is BoundingBoxes2D else np.asarray(PILImage.open(item._path)).tolist()
                    return {'file':item.file_is_set(),'bound':item.is_bound(),'payload':payload,
                        'labels':getattr(item,'_class_labels',getattr(item,'_val',{}).get('class_labels') if isinstance(getattr(item,'_val',{}),dict) else None),
                        'caption':item._caption,'type':item.type_name()}
                output['constructors'][name].append(result(construct))
                output['validators'][name].append(result(lambda value=copy.deepcopy(case):valid().validate(value)))
            item=valid()
            output['helpers'][name]={'array':result(item.to_data_array),'captions':result(lambda:cls.captions([item,item])),
                'empty_captions':result(lambda:cls.captions([])),
                'paths':[result(lambda p=p:cls.path_is_reference(p)) for p in [None,Path('http://a'),'s3://a','gs://a','http://a','https://a','ftp://a','file:///a','',{},1,b'http://a']],
                'missing_owner':result(lambda:item.to_json()),'invalid_owner':result(lambda:item.to_json(None))}
            item._caption='annotation caption'
            output['helpers'][name]['named_captions']=cls.captions([item,valid()])
            output['helpers'][name]['invalid_captions']=result(lambda:cls.captions([object()]))
            for i,(identity,ignore,link,symlink) in enumerate([(None,None,False,False),(0,False,False,False),('chosen',True,False,False),('linked',True,True,False),('symlink',True,True,True)]):
                item=cls(copy.deepcopy(cases[1]),'labels');run=ProbeRun(directory/f'{name}-{i}',link,symlink);other=ProbeRun(directory/f'{name}-{i}-other')
                artifact=w.Artifact(f'annotations-{name.lower()}-{i}','dataset')
                row={'unbound':result(lambda:item.to_json(run))}
                row['return']=item.bind_to_run(run,'nested/annotation',4,id_=identity,ignore_copy_err=ignore)
                row.update(json=item.to_json(run),bound=item.is_bound(),file=item.file_is_set(),singletons=copy.deepcopy(run.singletons),published=run.published.copy())
                row['wrong_owner']=result(lambda:item.to_json(other))
                row['same_file']=result(lambda:item.bind_to_run(run,'nested/annotation',4,id_=identity,ignore_copy_err=ignore))
                row['ignore_same']=item.bind_to_run(run,'nested/annotation',4,id_=identity,ignore_copy_err=True)
                row['artifact']=normalize(item.to_json(artifact));row['artifact_repeat']=normalize(item.to_json(artifact))
                if cls is ImageMask:
                    calls=[]
                    def entry(path):
                        calls.append(path)
                        return SimpleNamespace(download=lambda:item._path)
                    restored=cls.from_json(json_obj=row['artifact'],source_artifact=SimpleNamespace(get_entry=entry))
                    row['download_calls']=calls
                    row['restored_pixels']=np.asarray(PILImage.open(restored._path)).tolist()
                else:
                    restored=cls.from_json(json_obj=row['artifact'],source_artifact=artifact)
                    row['restored_boxes']=restored.to_json(artifact)
                item.bind_to_run(other,'cross',5,id_='other')
                row['cross_run']=item.to_json(other);row['old_run']=result(lambda:item.to_json(run))
                output['binding'][name].append(normalize(row))
            artifact=w.Artifact('annotation-decoding','dataset')
            inputs=[[],[box],{'box_data':[]},None] if cls is BoundingBoxes2D else [{},{'path':'missing.png'},None]
            def missing_entry(path): raise FileNotFoundError(path)
            owner = SimpleNamespace(get_entry=missing_entry) if cls is ImageMask else artifact
            output['decoding'][name]=[result(lambda value=value:cls.from_json(json_obj=value,source_artifact=owner).type_name()) for value in inputs]
        box_value=BoundingBoxes2D({'box_data':[copy.deepcopy(box)],'class_labels':{3:'name'}},'labels')
        payload=box_value.to_json(w.Artifact('mutate','dataset'));payload[0]['box_caption']='changed'
        output['box_mutation']={'same':payload is box_value._val,'public':box_value.to_json(w.Artifact('mutate-again','dataset')),'frozen':json.loads(Path(box_value._path).read_text())}
print(json.dumps(normalize(output),sort_keys=True,allow_nan=True))
