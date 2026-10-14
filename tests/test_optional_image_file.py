"""Image public contracts, durable files and independent process recovery.

Architecture: pinned isolated file matrix -> actual journals and artifact reads
-> failed uploads/restarts/delayed files -> shared Python/browser fixtures.
Main callers: local SDK suite and optional framework CI. No services or new
resources are created; optional image dependencies must already be installed.
"""
import base64,copy,hashlib,importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.apis.public.replay import reduce_events
from backprop.transport import sync
ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}

def make_image():
    import numpy as np
    from PIL import Image
    return w.Image(Image.new('RGB',(4,3),'red'),caption='immutable pixels',grouping=3,
        boxes={'truth':{'box_data':[{'position':{'minX':0,'minY':0,'maxX':1,'maxY':1},'class_id':7}],'class_labels':{7:'target'}}},
        masks={'seg':{'mask_data':np.array([[0,1],[1,0]],dtype=np.uint8),'class_labels':{0:'background',1:'foreground'}}})


class ImageFileContracts(unittest.TestCase):
    def setUp(self):
        try:import numpy;import PIL
        except ImportError:self.skipTest('Image file contracts require existing NumPy/Pillow')
        downloads=tempfile.TemporaryDirectory();self.addCleanup(downloads.cleanup)
        destination=patch.object(w.Artifact,'_default_root',lambda artifact,**kwargs:str(Path(downloads.name)/artifact._id))
        destination.start();self.addCleanup(destination.stop)

    def test_complete_portable_file_matrix_matches_pinned(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');values=[]
        for mode in ['reference','native']:
            p=subprocess.run([sys.executable,'-B',str(ROOT/'tests/image_file_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(p.returncode,0,p.stderr);values.append(json.loads(p.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'image-file-{mode}.json').write_text(p.stdout)
        self.assertEqual(*values)

    def test_binding_retry_artifact_copy_and_delayed_public_read(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();run=w.Run(id='image-files',entity='e',project='images',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
            self.addCleanup(run.finish)
            image=make_image();pixels=image._bytes()
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):image.bind_to_run(run,'nested/image',2,id_='chosen')
            image.bind_to_run(run,'nested/image',2,id_='chosen',ignore_copy_err=True)
            public=image.to_json(run);self.assertEqual(public['_type'],'image-file');self.assertEqual(public['format'],'png')
            self.assertEqual(Path(run.dir,public['path']).read_bytes(),pixels)
            self.assertIn('chosen0.boxes2D.json',public['boxes']['truth']['path']);self.assertIn('chosen00.mask.png',public['masks']['seg']['path'])
            artifact=w.Artifact('image-values','dataset');artifact.add(image,'image');run.log_artifact(artifact,aliases=['best']);run.log({'image':image})
            publish=sink.publish
            def lost_ack(events):publish(events);raise ConnectionError('lost acknowledgement')
            with patch.object(sink,'publish',side_effect=lost_ack):
                with self.assertRaises(w.Error):run.flush()
            run.flush();run.finish()
            self.assertEqual(reduce_events(sink.events),reduce_events(list(reversed(sink.events))+sink.events))
            api=w.Api._from_store(sink,entity='e',project='images');saved=api.artifact('image-values:best')
            with patch.object(sink,'download_file',side_effect=FileNotFoundError('not visible yet')):
                with self.assertRaises(FileNotFoundError):saved.get('image')
            restored=saved.get('image');self.assertEqual(restored._bytes(),pixels)
            target=w.Artifact('image-copy','dataset');descriptor=restored.to_json(target)
            self.assertEqual(target.get_entry(descriptor['path'])._read_verified_bytes(),pixels)
            decoded=w.Image.from_json(descriptor,target);self.assertEqual(decoded.to_data_array(),image.to_data_array())
            self.assertEqual(decoded.metadata['classes']._class_set,image.metadata['classes']._class_set)
            self.assertFalse(decoded.is_bound())
            second=w.Run(id=run.id,entity='e',project='images',dir=directory,_sink=sink,_deployment={},settings=SETTINGS,resume='must')
            self.addCleanup(second.finish)
            restored.bind_to_run(second,'restored',3,id_='again');second.log({'restored':restored});second.finish()
            self.assertEqual(len(list(api.run(run.id).scan_history())),2)
            self.assertEqual(sink.download_file(list(api.run(run.id).scan_history())[0]['image']),pixels)

    def test_process_exit_recovers_bound_pixels_annotations_and_labels(self):
        script='''
import json,sys
from pathlib import Path
import backprop as w
from test_optional_image_file import make_image,SETTINGS
run=w.Run(id='recovered-image',entity='e',project='images',mode='offline',dir=sys.argv[1],settings=SETTINGS)
image=make_image();image.bind_to_run(run,'image',1,id_='snapshot');public=image.to_json(run)
run.log({'image':image});run.finish()
print(json.dumps({'journal':run.sync_dir,'public':public}))
'''
        with tempfile.TemporaryDirectory() as directory:
            p=subprocess.run([sys.executable,'-B','-c',script,directory],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
            self.assertEqual(p.returncode,0,p.stderr);record=json.loads(p.stdout);sink=w.MemorySink()
            with patch.object(sink,'publish',side_effect=ConnectionError('disconnected')):
                with self.assertRaises(w.Error):sync(record['journal'],sink=sink)
            sync(record['journal'],sink=sink);sync(record['journal'],sink=sink)
            api=w.Api._from_store(sink,entity='e',project='images');run=api.run('recovered-image');row=list(run.scan_history())[0]
            self.assertEqual(run.state,'finished');self.assertEqual(row['image']['boxes']['truth']['class_labels'],{'7':'target'})
            self.assertEqual(run.rawconfig['_wandb']['mask/class_labels']['image_wandb_delimeter_seg']['value'],{'0':'background','1':'foreground'})
            self.assertEqual(run.file(record['public']['path']).size,len(sink.download_file(row['image'])))
            self.assertEqual(run.file(record['public']['masks']['seg']['path']).size,len(sink.download_file(row['image']['masks']['seg']['mask'])))

    def test_source_artifact_binding_skips_online_pixels_but_offline_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();image=make_image()
            with w.Run(id='source',entity='e',project='images',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
                artifact=w.Artifact('source-image','dataset');artifact.add(image,'image');run.log_artifact(artifact)
            saved=w.Api._from_store(sink,entity='e',project='images').artifact('source-image:latest')
            for offline in [False,True]:
                restored=saved.get('image');calls=[]
                with w.Run(id='target-'+str(offline),entity='e',project='images',dir=directory,_sink=sink,_deployment={},mode='offline' if offline else 'online',settings=SETTINGS) as run:
                    with patch.object(run,'_publish_file',wraps=run._publish_file) as publish:
                        restored.bind_to_run(run,'source',0,id_='copied');calls=[item.args[0] for item in publish.call_args_list]
                    self.assertEqual(restored.is_bound(),offline);self.assertEqual(len(calls),3 if offline else 2)
                    descriptor=restored.to_json(run);self.assertIn('artifact_path',descriptor)
                    self.assertEqual('path' in descriptor,offline)
                    run.log({'source':restored})

    def test_latest_reference_resolves_visible_alias_and_copies_frozen_bytes(self):
        from backprop.artifact_references import native_reference_entry
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink()
            with w.Run(id='latest-image',entity='e',project='images',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
                first=make_image();first.bind_to_run(run,'first',0,id_='one')
                artifact=w.Artifact('latest-images','dataset');artifact.add(first,'image')
                self.assertNotIn('_latest_artifact_path',first.to_json(run))
                run.log_artifact(artifact);run.flush()
                latest=first.to_json(run)['_latest_artifact_path'];immutable=first.to_json(run)['artifact_path']
                initial=native_reference_entry(SimpleNamespace(_store=sink),latest)._read_verified_bytes()
                earlier=copy.deepcopy(sink.events)
                second=make_image();second.caption='newer snapshot'
                next_artifact=w.Artifact('latest-images','dataset');next_artifact.add(second,'image');run.log_artifact(next_artifact);run.flush()
                new=native_reference_entry(SimpleNamespace(_store=sink),latest)._read_verified_bytes()
                self.assertNotEqual(initial,new)
                with patch.object(sink,'read_events',side_effect=lambda **kwargs:earlier):
                    self.assertEqual(native_reference_entry(SimpleNamespace(_store=sink),latest)._read_verified_bytes(),initial)
                self.assertEqual(native_reference_entry(SimpleNamespace(_store=sink),immutable)._read_verified_bytes(),initial)
                api=w.Api._from_store(sink,entity='e',project='images');old=api.artifact(artifact.id)
                old.aliases=['latest'];old.save()
                self.assertEqual(native_reference_entry(SimpleNamespace(_store=sink),latest)._read_verified_bytes(),initial)
                sink.events[:]=list(reversed(sink.events))+sink.events
                self.assertEqual(native_reference_entry(SimpleNamespace(_store=sink),latest)._read_verified_bytes(),initial)
                copied=w.Artifact('latest-copy','dataset');copied._store=sink
                copied.add_reference(latest,name='image.image-file.json')
                self.assertEqual(copied.get_entry('image.image-file.json')._read_verified_bytes(),initial)
                for suffix in ['?alias=best','?alias=latest&extra=1','?alias=latest#x']:
                    with self.assertRaises(ValueError):native_reference_entry(SimpleNamespace(_store=sink),immutable+suffix)



if __name__=='__main__':unittest.main()
