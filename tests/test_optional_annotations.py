"""Exercise complete annotation helpers with real arrays, images and file storage.

Architecture: isolated pinned/native comparisons -> real run and artifact flows
-> duplicate/restart replay. CPU Torch and available CUDA use the same public
constructors. Main callers: core SDK suite, optional CI and existing GPU hosts.
Missing optional dependencies are explicit skips, never conformance evidence.
"""
import contextlib
import copy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.media import BoundingBoxes2D,ImageMask
from backprop.apis.public.replay import reduce_events
from backprop.transport import BackpropError,read_journal

ROOT=Path(__file__).resolve().parents[1]
ARRAYS=all(importlib.util.find_spec(name) for name in ['numpy','PIL'])
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}


def box_data(class_id=3):
    return {'box_data':[{'position':{'minX':0,'minY':0,'maxX':1,'maxY':1},'class_id':class_id}],
            'class_labels':{class_id:f'object-{class_id}'}}


@unittest.skipUnless(ARRAYS,'Annotation contracts require optional NumPy and Pillow')
class AnnotationContracts(unittest.TestCase):
    def test_full_public_contracts_match_isolated_pinned_reference(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0')
        observations=[]
        for mode in ['reference','native']:
            proc=subprocess.run([sys.executable,'-B',str(ROOT/'tests/annotation_reference_probe.py'),mode],
                capture_output=True,text=True,timeout=50,cwd=tempfile.gettempdir(),
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(proc.returncode,0,proc.stderr)
            observations.append(json.loads(proc.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True)
                (out/f'annotations-{mode}.json').write_text(proc.stdout)
        self.assertEqual(observations[0],observations[1])

    @unittest.skipUnless(importlib.util.find_spec('torch'),'Optional Torch required')
    def test_tensor_conversion_matches_pinned_sdk_in_separate_processes(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0')
        values=[]
        for mode in ['reference','native']:
            proc=subprocess.run([sys.executable,'-B',str(ROOT/'tests/annotation_tensor_probe.py'),mode],
                capture_output=True,text=True,timeout=50,cwd=tempfile.gettempdir(),
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(proc.returncode,0,proc.stderr);values.append(json.loads(proc.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True)
                (out/f'annotation-tensors-{mode}.json').write_text(proc.stdout)
        self.assertEqual(values[0],values[1])

    def test_bound_annotations_publish_files_labels_and_retry_without_losing_other_metadata(self):
        import numpy as np
        with contextlib.ExitStack() as stack:
            directory=stack.enter_context(tempfile.TemporaryDirectory());sink=w.MemorySink()
            run=w.Run(id='annotation-binding',entity='e',project='annotations',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
            stack.callback(run.finish)
            run.config.update({'_wandb':{'keep':'reserved'}})
            boxes=BoundingBoxes2D(box_data(),'truth');mask=ImageMask({'mask_data':np.array([[0,255]],dtype=np.uint8),'class_labels':{0:'bg',255:'fg'}},'prediction')
            for item,identity in [(boxes,'boxes'),(mask,'mask')]:
                self.assertIsNone(item.bind_to_run(run,'image',7,id_=identity))
                self.assertTrue(item.file_is_set());self.assertTrue(item.is_bound())
                path=Path(run.dir)/item.to_json(run)['path'];self.assertTrue(path.is_file())
            config=run.config._data['_wandb'];self.assertEqual(config['keep'],'reserved')
            self.assertEqual(config['bounding_box/class_labels']['image_wandb_delimeter_truth']['value'],{'3':'object-3'})
            self.assertEqual(config['mask/class_labels']['image_wandb_delimeter_prediction']['value'],{'0':'bg','255':'fg'})
            count=sum(e['type']=='config' for e in read_journal(run.sync_dir))
            boxes.bind_to_run(run,'image',7,id_='boxes',ignore_copy_err=True)
            self.assertEqual(sum(e['type']=='config' for e in read_journal(run.sync_dir)),count)
            original=sink.publish
            def fail_after_write(events):original(events);raise OSError('lost annotation acknowledgement')
            with patch.object(sink,'publish',side_effect=fail_after_write):
                with self.assertRaises(BackpropError):run.flush()
            run.flush();run.finish()
            events=sink.events;state=reduce_events(events)
            self.assertEqual(state,reduce_events(list(reversed(events))+events))
            self.assertEqual(state['config']['_wandb'],config)
            restarted=w.Run(id=run.id,entity='e',project='annotations',dir=directory,_sink=sink,_deployment={},settings=SETTINGS,resume='must')
            stack.callback(restarted.finish)
            boxes.bind_to_run(restarted,'resumed',8,id_='copy')
            self.assertIn('image_wandb_delimeter_truth',restarted.config._data['_wandb']['bounding_box/class_labels'])
            self.assertIn('resumed_wandb_delimeter_truth',restarted.config._data['_wandb']['bounding_box/class_labels'])

    def test_native_artifacts_keep_labels_pixels_and_inline_image_layers(self):
        import numpy as np
        from PIL import Image
        boxes=BoundingBoxes2D(box_data(),'truth');labels={0:'background',255:'foreground'}
        mask=ImageMask({'mask_data':np.array([[0,255]],dtype=np.uint8),'class_labels':labels},'predictions')
        image=w.Image(np.zeros((1,2,3),dtype=np.uint8),boxes={'truth':boxes},masks={'predictions':mask})
        artifact=w.Artifact('annotations','dataset')
        artifact.add(boxes,'boxes');artifact.add(mask,'mask');artifact.add(image,'image')
        frozen={path:entry._read_verified_bytes() for path,entry in artifact._entries.items()}
        labels[255]='changed'
        self.assertEqual(artifact.get('boxes')._class_labels,{3:'object-3'})
        restored=artifact.get('mask');self.assertEqual(restored.metadata['class_labels'],{'0':'background','255':'foreground'})
        self.assertEqual(np.asarray(Image.open(restored._path)).tolist(),[[0,255]])
        picture=artifact.get('image')
        self.assertEqual(picture.metadata['boxes']['truth']['class_labels'],{'3':'object-3'})
        self.assertEqual(picture.metadata['masks']['predictions']['class_labels']['255'],'foreground')
        self.assertEqual({path:entry._read_verified_bytes() for path,entry in artifact._entries.items()},frozen)
        with contextlib.ExitStack() as stack:
            directory=stack.enter_context(tempfile.TemporaryDirectory());sink=w.MemorySink()
            run=w.Run(id='annotations',entity='e',project='annotations',dir=directory,_sink=sink,_deployment={},settings=SETTINGS);stack.callback(run.finish)
            run.log_artifact(artifact,aliases=['best']);run.log({'image':picture,'mask':restored,'boxes':artifact.get('boxes')});run.finish()
            api=w.Api._from_store(sink,entity='e',project='annotations');saved=api.artifact('annotations:best')
            self.assertEqual(saved.get('boxes')._class_labels,{3:'object-3'})
            self.assertEqual(saved.get('mask').metadata['class_labels']['255'],'foreground')
            self.assertEqual(len(list(api.run(run.id).scan_history())),1)

    def test_path_mask_labels_are_preserved_as_a_documented_reference_repair(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'labels.png';Image.fromarray(np.array([[1,2]],dtype=np.uint8)).save(path)
            mask=ImageMask({'path':path,'class_labels':{1:'first',2:'second'}},'path-labels')
            image=w.Image(path,masks={'path':mask})
            self.assertEqual(image.metadata['masks']['path']['class_labels'],{'1':'first','2':'second'})
            artifact=w.Artifact('path-labels','dataset');artifact.add(mask,'mask')
            self.assertEqual(artifact.get('mask').metadata['class_labels'],{'1':'first','2':'second'})

    def test_missing_optional_packages_have_reference_error_types(self):
        import numpy as np
        from backprop import annotation_values
        from backprop.tables import _optional_module
        def unavailable(name, purpose):
            if name=='numpy':raise ImportError('fixture numpy missing')
            return _optional_module(name,purpose)
        with patch('backprop.tables._optional_module',side_effect=unavailable):
            with self.assertRaisesRegex(w.Error,'Bounding box support requires numpy'):
                BoundingBoxes2D({'box_data':[]},'missing')
            with self.assertRaisesRegex(w.Error,'Image mask support requires numpy'):
                ImageMask({'mask_data':np.zeros((1,1))},'missing')
            with self.assertRaisesRegex(w.Error,'Image mask support requires numpy'):
                annotation_values.validate_mask({'mask_data':np.zeros((1,1))})
        def no_pillow(name,purpose):
            if name=='PIL.Image':raise ImportError('fixture Pillow missing')
            return _optional_module(name,purpose)
        with patch('backprop.tables._optional_module',side_effect=no_pillow):
            with self.assertRaisesRegex(w.Error,'PIL package'):
                ImageMask({'mask_data':np.zeros((1,1))},'missing')

    def test_concurrent_bindings_preserve_all_annotation_layers(self):
        from concurrent.futures import ThreadPoolExecutor
        with contextlib.ExitStack() as stack:
            directory=stack.enter_context(tempfile.TemporaryDirectory());sink=w.MemorySink()
            run=w.Run(id='threads',entity='e',project='annotations',dir=directory,_sink=sink,_deployment={},settings=SETTINGS);stack.callback(run.finish)
            def bind(index):
                value=BoundingBoxes2D(box_data(index),str(index));value.bind_to_run(run,'image-'+str(index),index,id_='boxes');return value
            with ThreadPoolExecutor(max_workers=4) as pool:values=list(pool.map(bind,range(16)))
            self.assertEqual(len({value._path for value in values}),16)
            self.assertEqual(len(run.config._data['_wandb']['bounding_box/class_labels']),16)
            run.finish();self.assertEqual(len(reduce_events(sink.events)['config']['_wandb']['bounding_box/class_labels']),16)

    @unittest.skipUnless(importlib.util.find_spec('torch'),'Optional Torch required')
    def test_torch_cpu_input_mutations_and_gradient_error(self):
        self._torch_inputs('cpu')

    @unittest.skipUnless(importlib.util.find_spec('torch'),'Optional Torch required')
    def test_torch_cuda_input_transfer_and_encoding(self):
        import torch
        if not torch.cuda.is_available():self.skipTest('Existing CUDA device required')
        self._torch_inputs('cuda')

    def _torch_inputs(self,device):
        import numpy as np
        import torch
        from PIL import Image
        center=torch.tensor([.25,.75],device=device,requires_grad=True)
        data={'box_data':[{'position':{'middle':center,'width':.5,'height':.25},'class_id':2}]}
        box=BoundingBoxes2D(data,'tensor');self.assertEqual(data['box_data'][0]['position']['middle'],[.25,.75]);self.assertTrue(box.file_is_set())
        pixels=torch.tensor([[0,127,128,255]],device=device,dtype=torch.uint8)
        data={'mask_data':pixels};mask=ImageMask(data,'tensor')
        self.assertIsInstance(data['mask_data'],np.ndarray)
        self.assertEqual(np.asarray(Image.open(mask._path)).tolist(),[[0,127,128,255]])
        with self.assertRaises(RuntimeError):ImageMask({'mask_data':torch.ones((1,2),device=device,requires_grad=True)},'gradient')
        if device=='cuda':torch.cuda.synchronize()


if __name__=='__main__':unittest.main()
