"""Portable artifact references, acknowledgement gates and retry/restart reads.

Architecture: isolated pinned/native fixture -> real durable publication ->
independent public lookup/config resolution. Main callers: SDK suite and CI.
Identity substitutions remain explicit; no hosted W&B response is fabricated.
"""
import copy,importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.artifact_identity import ArtifactNotLoggedError
ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}


class ArtifactIdentityContracts(unittest.TestCase):
    def test_pinned_schema_signatures_aliases_and_error_order(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel is unavailable')
        self.assertEqual(reference.version,'0.30.0');records=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_identity_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=45,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
                (folder/f'artifact-identity-{mode}.json').write_text(result.stdout)
        self.assertEqual(records[0]['imports'],records[1]['imports'])
        for group in records[1]['imports'].values():
            self.assertEqual(group['committed']['json_encode']['value'],{'_type':'artifactVersion','_version':'v0',
                'id':'artifact-identity','version':'sha256-test-version','sequenceName':'dataset','usedAs':None})
            for stage in ['draft','finalized','queued']:
                self.assertEqual(group[stage]['json_encode']['error'],'ArtifactNotLoggedError')
                self.assertTrue(group[stage]['json_encode']['owns_object'])

    def test_acknowledgement_retry_edits_and_independent_config_resolution(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR':directory}):
            sink=w.MemorySink();run=w.init(id='identity',entity='e',project='identity',dir=directory,
                _sink=sink,_deployment={},settings=SETTINGS)
            self.addCleanup(run.finish)
            artifact=w.Artifact('dataset','data',metadata={'revision':1})
            file=Path(directory)/'weights.bin';file.write_bytes(b'original weights');artifact.add_file(file)
            run.log_artifact(artifact)
            with self.assertRaises(ArtifactNotLoggedError):artifact.json_encode()
            publish=sink.publish
            def lost_ack(events):publish(events);raise OSError('lost acknowledgement')
            with patch.object(sink,'publish',side_effect=lost_ack):
                with self.assertRaises(w.Error):run.flush()
            with self.assertRaises(ArtifactNotLoggedError):_ = artifact.commit_hash
            run.flush();encoded=artifact.json_encode();commit=artifact.commit_hash
            self.assertEqual(commit,artifact.digest);self.assertEqual(encoded['version'],'sha256-'+commit)
            artifact.metadata['revision']=2;artifact.description='edited';artifact.save()
            self.assertEqual(artifact.json_encode(),encoded);self.assertEqual(artifact.commit_hash,commit)
            api=w.Api._from_store(sink,entity='e',project='identity')
            found=api.artifact(encoded['id']);self.assertEqual(found.json_encode(),encoded)
            events=copy.deepcopy(sink.events);sink.events=[]
            with self.assertRaises(w.CommError):api.artifact(encoded['id'])
            sink.events=list(reversed(events))+events
            self.assertEqual(api.artifact(encoded['id']).commit_hash,commit)
            run.finish()
            with w.init(id='consumer',entity='e',project='identity',dir=directory,config={'dataset':encoded},
                    _sink=sink,_deployment={},settings=SETTINGS) as consumer:
                self.assertIsInstance(consumer.config['dataset'],w.Artifact)
                self.assertEqual(consumer.config['dataset'].id,encoded['id'])
            newer=w.Artifact('dataset','data');file.write_bytes(b'new weights');newer.add_file(file)
            with w.init(id='newer',entity='e',project='identity',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as owner:
                owner.log_artifact(newer);owner.flush()
                self.assertNotEqual(newer.commit_hash,commit)

    def test_offline_process_restart_recovers_the_same_reference(self):
        script='''
import json,sys
from pathlib import Path
import backprop as w
from test_artifact_identity import SETTINGS
with w.init(id='offline-identity',entity='e',project='identity',dir=sys.argv[1],mode='offline',_deployment={},settings=SETTINGS) as run:
 artifact=w.Artifact('dataset','data');path=Path(sys.argv[1])/'values.txt';path.write_text('durable')
 artifact.add_file(path);run.log_artifact(artifact)
 print(json.dumps({'id':artifact._id,'version':artifact.version,'digest':artifact.digest,'journal':run.sync_dir}),flush=True)
'''
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR':directory}):
            result=subprocess.run([sys.executable,'-B','-c',script,directory],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,
                env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
            self.assertEqual(result.returncode,0,result.stderr);record=json.loads(result.stdout)
            sink=w.MemorySink();w.sync(record['journal'],sink=sink);w.sync(record['journal'],sink=sink)
            found=w.Api._from_store(sink,entity='e',project='identity').artifact(record['id'])
            self.assertEqual(found.json_encode()['version'],record['version'])
            self.assertEqual(found.commit_hash,record['digest'])
            self.assertEqual(next(iter(found.manifest.entries.values()))._read_verified_bytes(),b'durable')
