"""Verify accepted contribution handles and completed union reconstruction.

Architecture: genuine pinned comparisons -> online/offline lifecycle -> delayed
read view and malformed completion -> retries/deadlines -> real process recovery.
Main callers: SDK release suite. The browser consumes the same recovery events.
"""
import copy,importlib.metadata,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.artifact_identity import ArtifactNotLoggedError,WaitTimeoutError
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ContributionContracts(ContextTestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.folder)}))
        self.sink=w.MemorySink()

    def run_handle(self,name,offline=False):
        run=w.init(id=name,entity='e',project='p',group='parts',mode='offline' if offline else 'online',
            dir=str(self.folder/name),_sink=None if offline else self.sink,_deployment={},settings=SETTINGS,reinit='create_new')
        self.addCleanup(lambda:run.finish() if not run._closed else None)
        return run

    def part(self,name='worker',**kwargs):
        artifact=w.Artifact('model','model',**kwargs)
        with artifact.new_file(name) as out:out.write(name)
        return artifact

    def test_pinned_return_states_and_public_imports(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0')
        evidence={}
        for module in ['wandb','wandb.sdk','wandb.sdk.artifacts.artifact']:
            observations=[]
            for mode in ['reference','native']:
                child=subprocess.run([sys.executable,'-B',str(ROOT/'tests/distributed_contribution_handle_probe.py'),mode,module],
                    cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=40,
                    env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
                self.assertEqual(child.returncode,0,child.stderr)
                result=json.loads(child.stdout);result.pop('source_sha256',None);observations.append(result)
            self.assertEqual(observations[0],observations[1]);evidence[module]=observations
        if os.environ.get('CI'):
            out=ROOT/'test-results';out.mkdir(exist_ok=True)
            (out/'distributed-artifact-contribution-handles.json').write_text(json.dumps(evidence))

    def test_delayed_duplicate_view_combined_files_and_local_edits(self):
        worker=self.run_handle('worker');artifact=self.part(incremental=True)
        artifact.add(w.Table(columns=['x'],data=[[1]]),'table')
        entry=artifact.get_entry('worker');self.assertIs(worker.upsert_artifact(artifact),artifact)
        original=Path(worker.sync_dir,'events.jsonl').read_bytes()
        self.assertIs(artifact.wait(),artifact);self.assertTrue(artifact.is_draft());self.assertIsNone(artifact.id)
        worker.finish();original=Path(worker.sync_dir,'events.jsonl').read_bytes()
        primary=self.run_handle('primary');final=self.part('primary',metadata={'owner':'primary'},description='primary')
        primary.finish_artifact(final,aliases=['best']);primary.finish()
        read=self.sink.read_events
        with patch.object(self.sink,'read_events',side_effect=lambda **scope:[e for e in read(**scope) if e['type']!='artifact']):
            self.assertIs(artifact.wait(),artifact);self.assertEqual(artifact.state,'PENDING')
        artifact.description='edited before commitment'
        with patch.object(self.sink,'read_events',side_effect=lambda **scope:list(reversed(read(**scope)))+read(**scope)):
            self.assertIs(artifact.wait(timeout=5),artifact)
        self.assertEqual(artifact.id,final.id);self.assertTrue(artifact.incremental)
        self.assertIs(artifact.get_entry('worker'),entry);self.assertEqual(entry._read_verified_bytes(),b'worker')
        self.assertEqual(artifact.get('table').data,[[1]])
        self.assertEqual(set(artifact.manifest.entries),{'worker','primary','table.table.json'})
        self.assertEqual(artifact.metadata,{'owner':'primary'});self.assertEqual(artifact.description,'edited before commitment')
        self.assertEqual(set(artifact.aliases),{'best','latest'})
        self.assertEqual(Path(worker.sync_dir,'events.jsonl').read_bytes(),original)
        artifact.save();api=w.Api._from_store(self.sink,entity='e',project='p')
        self.assertEqual(api.artifact(artifact.id).description,'edited before commitment')
        with self.run_handle('consumer') as consumer:
            self.assertIs(consumer.use_artifact(artifact),artifact)
            self.assertIs(consumer.log_artifact(artifact),artifact)
            consumer.config['model']=artifact
        public=api.run('consumer')
        self.assertEqual([a.id for a in public.used_artifacts()],[artifact.id])
        self.assertEqual([a.id for a in public.logged_artifacts()],[artifact.id])

    def test_lost_ack_wait_recovers_same_part_and_offline_wait_rejects(self):
        worker=self.run_handle('worker');artifact=self.part();publish=self.sink.publish
        def lost(events):
            publish(events)
            if any(e['type']=='artifact_part' for e in events):raise OSError('lost contribution response')
        with patch.object(self.sink,'publish',side_effect=lost):
            with self.assertRaises(w.Error):worker.upsert_artifact(artifact)
        source=Path(worker.sync_dir,'events.jsonl').read_bytes()
        self.assertIs(artifact.wait(timeout=5),artifact);self.assertEqual(artifact.state,'PENDING')
        self.assertEqual(len({e['event_id'] for e in self.sink.events if e['type']=='artifact_part'}),1)
        self.assertEqual(Path(worker.sync_dir,'events.jsonl').read_bytes(),source)
        self.assertIs(self.run_handle('next-worker').upsert_artifact(artifact),artifact)
        offline=self.run_handle('offline',True);pending=offline.upsert_artifact(self.part('offline'))
        with self.assertRaises(ArtifactNotLoggedError):pending.wait()

    def test_read_timeout_cannot_commit_in_background(self):
        worker=self.run_handle('worker');artifact=worker.upsert_artifact(self.part())
        primary=self.run_handle('primary');primary.finish_artifact(self.part('primary'))
        release=threading.Event();entered=threading.Event();read=self.sink.read_events
        def slow(**scope):entered.set();release.wait(5);return read(**scope)
        with patch.object(self.sink,'read_events',side_effect=slow):
            try:
                with self.assertRaises(WaitTimeoutError):artifact.wait(timeout=.01)
                self.assertTrue(entered.is_set());self.assertEqual(artifact.state,'PENDING')
            finally:release.set()
        self.assertIs(artifact.wait(timeout=5),artifact);self.assertEqual(artifact.state,'COMMITTED')

    def test_invalid_union_raises_without_mutating_handle(self):
        worker=self.run_handle('worker');artifact=worker.upsert_artifact(self.part())
        primary=self.run_handle('primary');primary.finish_artifact(self.part('primary'))
        original=copy.deepcopy(self.sink.events)
        for event in self.sink.events:
            if event['type']=='artifact':event['payload']['files']=[ref for ref in event['payload']['files'] if ref['name']!='worker']
        with self.assertRaisesRegex(ValueError,'original contribution file'):artifact.wait()
        self.assertEqual(artifact.state,'PENDING');self.assertEqual(set(artifact.manifest.entries),{'worker'})
        self.sink.events=original;self.assertIs(artifact.wait(),artifact)

    def test_same_object_writer_scope_and_explicit_final_publication(self):
        first=self.run_handle('first');second=self.run_handle('second');artifact=self.part()
        first.upsert_artifact(artifact);second.upsert_artifact(artifact)
        self.assertEqual(len({e['payload']['part_id'] for e in self.sink.events if e['type']=='artifact_part'}),2)
        self.assertIs(second.finish_artifact(artifact),artifact)
        self.assertEqual(len({e['payload']['part_id'] for e in self.sink.events if e['type']=='artifact_part'}),2)
        self.assertIs(artifact.wait(),artifact);self.assertEqual(artifact.state,'COMMITTED')
        ordinary=self.part('ordinary');first.upsert_artifact(ordinary);first.log_artifact(ordinary)
        self.assertIs(ordinary.wait(),ordinary);self.assertEqual(ordinary.state,'COMMITTED')

    def test_late_handle_waits_for_its_own_generation(self):
        first=self.run_handle('first');early=first.upsert_artifact(self.part('early'))
        primary=self.run_handle('primary');one=primary.finish_artifact(self.part('one'))
        second=self.run_handle('second');late=second.upsert_artifact(self.part('late'))
        self.assertIs(late.wait(),late);self.assertEqual(late.state,'PENDING')
        self.assertIs(early.wait(),early);self.assertEqual(early.id,one.id)
        two=primary.finish_artifact(self.part('two'))
        self.assertIs(late.wait(),late);self.assertEqual(late.id,two.id)
        self.assertEqual(set(late.manifest.entries),{'late','two'})
        self.assertIs(early.wait(timeout=-1),early);self.assertEqual(early.id,one.id)

    def test_process_exit_and_labeled_handle_recovery(self):
        from contribution_handle_replay_fixture import build
        result=build();self.assertTrue(result['hard_exit_recovered']);self.assertTrue(result['delayed_completion_pending'])
