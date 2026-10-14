"""Compare actual CPU/CUDA annotation conversion in isolated SDK processes.

Architecture: selected reference/native import -> real Torch tensors -> saved
JSON/PNG pixels and input mutations. Main callers: optional tests and existing
GPU verification. The pinned source import needs no running W&B service.
"""
import hashlib
import inspect
import json
from pathlib import Path
import sys

import numpy as np
import torch
from PIL import Image

if sys.argv[1]=='reference':
    import wandb
    assert wandb.__version__=='0.30.0'
    from wandb.sdk.data_types.helper_types.bounding_boxes_2d import BoundingBoxes2D
    from wandb.sdk.data_types.helper_types.image_mask import ImageMask
    for cls,digest in [(BoundingBoxes2D,'b190e042546af2093a21b9e0dd454b3a7d261bb34f9e2db6d3fe7724e9ab06b0'),(ImageMask,'7598f0d5552fc6dd9f216b51f6bbe6849cca4db88d51334d7f58002194eebd81')]:
        assert hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()==digest
else:
    from backprop.media import BoundingBoxes2D,ImageMask

output={}
for device in ['cpu']+(['cuda'] if torch.cuda.is_available() else []):
    center=torch.tensor([.25,.75],device=device,requires_grad=True)
    data={'box_data':[{'position':{'middle':center,'width':.5,'height':.25},'class_id':2}]}
    boxes=BoundingBoxes2D(data,'tensor')
    value={'box_input':data,'box_file':json.loads(Path(boxes._path).read_text()),'masks':[]}
    for pixels in [torch.tensor([[0,127,128,255]],device=device,dtype=torch.uint8),torch.tensor([[.1,1.9,128.5,255]],device=device)]:
        data={'mask_data':pixels}
        mask=ImageMask(data,'tensor')
        value['masks'].append({'mutated_to_numpy':isinstance(data['mask_data'],np.ndarray),'input':data['mask_data'].tolist(),
            'labels':data['class_labels'],'pixels':np.asarray(Image.open(mask._path)).tolist()})
    try:ImageMask({'mask_data':torch.ones((1,2),device=device,requires_grad=True)},'gradient')
    except Exception as error:value['gradient_error']=type(error).__name__
    else:raise AssertionError('Gradient tensor unexpectedly accepted')
    if device=='cuda':torch.cuda.synchronize()
    output[device]=value
print(json.dumps(output,sort_keys=True))
