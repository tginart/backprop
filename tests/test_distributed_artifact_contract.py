"""Verify portable distributed APIs plus deferred union and retry semantics.

Architecture: pinned comparison -> actual offline/online calls -> durable sync
and visible-part queries. Main caller: SDK regression suite. Independent process
replay additionally checks the same emitted envelopes in the browser reducer.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class DistributedArtifactContracts(ContextTestCase):
    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.directory)}));self.sink=w.MemorySink()

    def run_handle(self,name,offline=False):
        run=w.init(id=name,entity='e',project='p',group='workers',reinit='create_new',mode='offline' if offline else 'online',dir=str(self.directory/name),
            _sink=None if offline else self.sink,_deployment={},settings=SETTINGS)
        self.addCleanup(lambda:run.finish() if not run._closed else None);return run

    def part(self,path='own',text='own'):
        artifact=w.Artifact('model','model',incremental=True)
        with artifact.new_file(path) as out:out.write(text)
        return artifact

    def test_pinned_public_calls_across_aliases_and_modes(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');results=[]
        for mode in ['reference','native']:
            child=subprocess.run([sys.executable,'-B',str(ROOT/'tests/distributed_artifact_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(child.returncode,0,child.stderr);results.append(json.loads(child.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'distributed-artifact-{mode}.json').write_text(child.stdout)
        results[0].pop('source_sha256');self.assertEqual(len(results[0]),337);self.assertEqual(results[0],results[1])

    def test_offline_union_refreshes_same_object_and_config_after_sync(self):
        worker=self.run_handle('worker',True);worker.upsert_artifact(self.part('worker','other'));worker.finish()
        primary=self.run_handle('primary',True);artifact=self.part();entry=artifact.manifest.entries['own']
        self.assertIs(primary.finish_artifact(artifact),artifact);primary.config['model']=artifact;primary.finish()
        original=Path(primary.sync_dir,'events.jsonl').read_bytes()
        self.assertEqual(artifact.state,'PENDING');self.assertTrue(artifact.incremental)
        w.sync(worker.sync_dir,sink=self.sink);w.sync(primary.sync_dir,sink=self.sink)
        self.assertEqual(artifact.state,'COMMITTED');self.assertIs(artifact.manifest.entries['own'],entry)
        self.assertEqual(set(artifact.manifest.entries),{'own','worker'})
        self.assertEqual(Path(primary.sync_dir,'events.jsonl').read_bytes(),original)
        api=w.Api._from_store(self.sink,entity='e',project='p');found=api.artifact('model:latest')
        self.assertEqual(found.version,artifact.version)
        self.assertEqual(api.run('primary').config['model']['version'],found.version)
        self.assertEqual(found.get_entry('worker')._read_verified_bytes(),b'other')

    def test_identical_new_drafts_after_completed_round_are_new_operations(self):
        primary=self.run_handle('primary');first=self.part();second=self.part()
        self.assertIs(primary.finish_artifact(first),first);self.assertIs(primary.finish_artifact(second),second)
        finals=[e['payload'] for e in self.sink.events if e['type']=='artifact']
        self.assertEqual(len(finals),2);self.assertNotEqual(finals[0]['operation_id'],finals[1]['operation_id'])
        self.assertNotEqual(first.id,second.id);self.assertEqual(first.version,second.version)
        self.assertTrue(set(finals[0]['contribution_ids']).isdisjoint(finals[1]['contribution_ids']))

    def test_pending_part_retry_reuses_durable_identity(self):
        worker=self.run_handle('worker');publish=self.sink.publish
        def lose(events):
            publish(events)
            if any(e['type']=='artifact_part' for e in events):raise OSError('lost acknowledgment')
        with patch.object(self.sink,'publish',side_effect=lose):
            with self.assertRaises(w.Error):worker.upsert_artifact(self.part())
        original=next(e for e in self.sink.events if e['type']=='artifact_part')
        worker.upsert_artifact(self.part())
        parts=[e for e in self.sink.events if e['type']=='artifact_part']
        self.assertEqual({e['event_id'] for e in parts},{original['event_id']})

    def test_finalization_crash_freezes_visible_parts_and_recovers_in_browser(self):
        from distributed_artifact_replay_fixture import build
        result=build();self.assertTrue(result['hard_exit_recovered']);self.assertTrue(result['late_part_retained'])

    def test_committed_input_keeps_its_identity_while_union_is_another_output(self):
        primary=self.run_handle('primary');source=self.part();primary.log_artifact(source);primary.flush()
        identity,version=source.id,source.version;entry=source.manifest.entries['own']
        worker=self.run_handle('worker');worker.upsert_artifact(self.part('worker','other'));worker.finish()
        self.assertIs(primary.finish_artifact(source),source)
        self.assertEqual((source.id,source.version),(identity,version));self.assertEqual(source.state,'COMMITTED')
        self.assertEqual(set(source.manifest.entries),{'own'});self.assertIs(source.manifest.entries['own'],entry)
        api=w.Api._from_store(self.sink,entity='e',project='p');union=api.artifact('model:latest')
        self.assertNotEqual(union.id,identity);self.assertEqual(set(union.manifest.entries),{'own','worker'})
        self.assertEqual(set(api.artifact(identity).manifest.entries),{'own'})
