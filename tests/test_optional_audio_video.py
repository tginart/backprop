"""Audio/Video public file parity and native durable media behavior.

Architecture: independent pinned/native processes -> file/URI contracts ->
ordinary journals and artifacts -> retry/restart reconstruction. Source owner
fixtures substitute only unavailable W&B service objects, never value methods.
Main callers: full SDK and optional CI. No packages are installed by tests.
"""
import base64
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop as w
from backprop.apis.public.replay import reduce_events
from backprop.transport import BackpropError, sync

ROOT=Path(__file__).resolve().parents[1]
SETTINGS={"silent":True,"console":"off","disable_git":True,"x_disable_stats":True,"flush_interval":600}
GIF=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')


class AudioVideoContracts(unittest.TestCase):
    def test_public_members_match_pinned_at_all_three_import_paths(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/audio_video_reference_probe.py'),mode],
                capture_output=True,text=True,timeout=60,cwd=tempfile.gettempdir(),
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),
                     'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'audio-video-{mode}.json').write_text(result.stdout)
        self.assertEqual(observations[0].pop('source_hashes'),{'Audio':'9cd5ec160d3799c57934024838a4da3abafda117daa910f5720fe2888411b640','Video':'364916680428208f4299d21f096ade0c175e2f70bee8b26c8899818310491dc4'})
        self.assertEqual(observations[0],observations[1])

    def test_real_files_public_decoding_native_roundtrips_and_restart(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(w.Artifact,'_default_root',lambda a,**kw:str(Path(directory,'downloads',a._name))):
            sink=w.MemorySink();run=w.Run(id='audio-video',entity='e',project='media',dir=directory,_sink=sink,_deployment={},settings=SETTINGS);self.addCleanup(run.finish)
            audio=w.Audio([0,.5,-.5],sample_rate=8000,caption='clip');video=w.Video(io.BytesIO(GIF),format='gif',caption='animation')
            expected={'audio':audio._bytes(),'video':video._bytes()};artifact=w.Artifact('playback','dataset')
            for key,value in [('audio',audio),('video',video)]:
                self.assertTrue(value.file_is_set());self.assertFalse(value.is_bound())
                value.bind_to_run(run,key,1,id_='chosen');descriptor=value.to_json(run)
                self.assertEqual(Path(run.dir,descriptor['path']).read_bytes(),expected[key])
                public=value.to_json(artifact);restored=type(value).from_json(public,artifact)
                self.assertEqual(restored._bytes(),expected[key]);self.assertFalse(restored.is_bound())
                if key=='video':self.assertIsNone(restored.caption) # Pinned inherited decoder ignores captions.
                else:
                    self.assertEqual(restored.caption,'clip');self.assertEqual(w.Audio.sample_rates([restored]),[None])
                    with self.assertRaises(AttributeError):restored.resolve_ref()
                artifact.add(value,key)
            run.log_artifact(artifact,aliases=['best']);run.log({'audio':audio,'video':video})
            publish=sink.publish
            def lose_ack(events):publish(events);raise OSError('lost media acknowledgement')
            with patch.object(sink,'publish',side_effect=lose_ack):
                with self.assertRaises(BackpropError):run.flush()
            run.flush();run.finish();self.assertEqual(reduce_events(sink.events),reduce_events(list(reversed(sink.events))+sink.events))
            api=w.Api._from_store(sink,entity='e',project='media');saved=api.artifact('playback:best');row=list(api.run(run.id).scan_history())[0]
            for key in ['audio','video']:
                self.assertEqual(sink.download_file(row[key]),expected[key]);restored=saved.get(key)
                self.assertEqual(restored._bytes(),expected[key]);self.assertEqual(restored.caption,'clip' if key=='audio' else 'animation')
                destination=w.Artifact('copy-'+key,'dataset');descriptor=restored.to_json(destination)
                self.assertEqual(destination.get_entry(descriptor['path'])._read_verified_bytes(),expected[key])
            self.assertIsNone(saved.get('audio').resolve_ref())
            restarted=w.Run(id=run.id,entity='e',project='media',dir=directory,_sink=sink,_deployment={},settings=SETTINGS,resume='must');self.addCleanup(restarted.finish)
            restarted.log({'audio':saved.get('audio'),'video':saved.get('video')});restarted.finish()
            self.assertEqual(len(list(api.run(run.id).scan_history())),2)

    def test_audio_batch_uses_declared_metadata_and_needs_no_soundfile_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            raw=w.Audio([0,.5],sample_rate=8000);path=Path(directory,'input.wav');path.write_bytes(raw._bytes())
            file=w.Audio(path,sample_rate=16000,caption=False)
            self.assertEqual(w.Audio.sample_rates([file]),[16000]);self.assertEqual(w.Audio.durations([file]),[None])
            self.assertEqual(file.metadata['sample_rate'],8000);self.assertEqual(file.metadata['duration'],2/8000)
            self.assertEqual(w.Audio.captions([file]),[False])
            # Unlike W&B, batching already encoded files performs no SoundFile
            # import. Standard-library encoding supplies the complete operation.
            with patch.dict(sys.modules,{'soundfile':None}):
                self.assertEqual(w.Audio.seq_to_json([],SimpleNamespace(dir=directory),'x',0),{'_type':'audio','count':0,'audio':[]})

    def test_generated_media_offline_journal_survives_encoder_process_exit(self):
        script = """
import json,io,sys
import backprop as w
root=sys.argv[1]
with w.Run(id='producer',entity='e',project='temporary-media',dir=root,mode='offline',settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'allow_media_symlink':True,'symlink':True}) as run:
    audio=w.Audio([0,.5,-.5],8000,caption='clip')
    video=w.Video(io.BytesIO(b'encoded-video'),format='webm',caption='clip')
    html=w.Html('<p>durable</p>',inject=False)
    run.log({'audio':audio,'video':video,'html':html,'batch':[audio,audio]})
    print(json.dumps({'directory':run.sync_dir,'temporary':[audio._path,video._path,html._path]}))
"""
        with tempfile.TemporaryDirectory() as directory:
            process=subprocess.run([sys.executable,'-B','-c',script,directory],capture_output=True,text=True,
                cwd=tempfile.gettempdir(),env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')},timeout=30)
            self.assertEqual(process.returncode,0,process.stderr);result=json.loads(process.stdout)
            self.assertTrue(all(not Path(path).exists() for path in result['temporary']))
            sink=w.MemorySink();sync(result['directory'],sink=sink)
            row=next(e['payload']['metrics'] for e in sink.events if e['type']=='history')
            self.assertEqual(sink.download_file(row['audio']),w.Audio([0,.5,-.5],8000)._bytes())
            self.assertEqual(sink.download_file(row['video']),b'encoded-video')
            self.assertEqual(sink.download_file(row['html']),b'<p>durable</p>')
            self.assertEqual(sink.download_file(row['batch'][0]),sink.download_file(row['audio']))

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ['numpy','moviepy','imageio','PIL']),'Optional video encoder unavailable')
    def test_video_reencoding_refreshes_actual_file_and_dimensions(self):
        import numpy as np
        frames=np.zeros((2,3,4,4),dtype=np.uint8);frames[1]=255
        value=w.Video(frames,format='gif',fps=4,caption='frames');first=value._path
        self.assertEqual(value._bytes(),value.data)
        value.encode(fps=8);self.assertNotEqual(value._path,first);self.assertTrue(value.file_is_set())
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink()
            with w.Run(id='encoded',project='media',dir=directory,_sink=sink,_deployment={},settings={**SETTINGS,'allow_media_symlink':True,'symlink':True}) as run:
                value.bind_to_run(run,'frames',0);descriptor=value.to_json(run)
                self.assertEqual([descriptor['width'],descriptor['height']],[4,4])
                self.assertEqual(Path(run.dir,descriptor['path']).read_bytes(),value.data)
                first_bytes=value._bytes();run.log({'video':value});run.flush()
                value.encode(fps=2);second_bytes=value._bytes();run.log({'video':value})
            rows=[e['payload']['metrics']['video'] for e in sink.events if e['type']=='history']
            self.assertNotEqual(rows[0]['path'],rows[1]['path'])
            self.assertEqual(sink.download_file(rows[0]),first_bytes)
            self.assertEqual(sink.download_file(rows[1]),second_bytes)


if __name__=='__main__':unittest.main()
