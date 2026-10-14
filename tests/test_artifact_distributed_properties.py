"""Verify descriptor contracts independently of unfinished distributed methods.

Architecture: isolated pinned/native observations -> public logging and queries
-> local-only committed assignment -> immutable stream comparison. Main caller:
SDK regression CI. A green property does not claim its constructor or every
publication method is complete.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class DistributedArtifactProperties(unittest.TestCase):
    def test_pinned_descriptors_and_import_aliases(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');results=[]
        for mode in ['reference','native']:
            child=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_distributed_properties_probe.py'),mode],
                cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=40,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(child.returncode,0,child.stderr);results.append(json.loads(child.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'distributed-artifact-properties-{mode}.json').write_text(child.stdout)
        results[0].pop('source_sha256')
        self.assertEqual(results[0],results[1])
        self.assertEqual(sum(len(v)-1 for v in results[0].values()),450)

    def test_public_reads_and_committed_local_changes_do_not_edit_history(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            sink=w.MemorySink()
            with w.init(id='primary',entity='e',project='p',group='workers',mode='online',dir=directory,
                    _sink=sink,_deployment={},settings=SETTINGS) as run:
                with patch.object(w,'termwarn'):artifact=w.Artifact('model','model',incremental=True)
                with artifact.new_file('weights') as out:out.write('weights')
                run.finish_artifact(artifact)
                self.assertTrue(artifact.incremental);self.assertEqual(artifact.distributed_id,'workers')
                self.assertEqual(artifact.state,'COMMITTED')
            api=w.Api._from_store(sink,entity='e',project='p');restored=api.artifact('model:latest')
            self.assertEqual(restored.distributed_id,'workers');self.assertFalse(restored.incremental)
            original=json.dumps(sink.events,sort_keys=True)
            for value in [None,'new',{},[]]:
                restored.distributed_id=value;self.assertIs(restored.distributed_id,value)
            self.assertEqual(json.dumps(sink.events,sort_keys=True),original)
            self.assertEqual(api.artifact('model:latest').distributed_id,'workers')

    def test_invalid_public_group_requests_still_fail_before_recording_parts(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            with w.init(id='r',entity='e',project='p',mode='offline',dir=directory,_deployment={},settings=SETTINGS) as run:
                before=Path(run.sync_dir,'events.jsonl').read_bytes()
                for method in [run.upsert_artifact,run.finish_artifact]:
                    with self.assertRaises(TypeError):method(w.Artifact('model','model'),distributed_id=42)
                self.assertEqual(Path(run.sync_dir,'events.jsonl').read_bytes(),before)
