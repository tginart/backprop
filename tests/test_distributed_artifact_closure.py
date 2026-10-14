"""Exercise durable consumed-part decisions across delayed queries and races.

Architecture: actual public calls and immutable journals -> staged part delivery
-> group file claims -> retry/restart -> independent readback. Faults replace only
transport visibility/acknowledgments. A separate process fixture and browser test
verify canonical IDs after concurrent finalization and a hard process exit.
"""
import copy,json,os,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.transport import Journal,read_journal
from backprop.distributed_artifact_closure import KIND
from test_artifact_identity import SETTINGS


class DistributedClosure(ContextTestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.root)}))
        self.sink=w.MemorySink()

    def run_handle(self,name,offline=False,group='workers',project='p'):
        return w.init(id=name,entity='e',project=project,group=group,mode='offline' if offline else 'online',
            dir=str(self.root/name),_sink=None if offline else self.sink,_deployment={},settings=SETTINGS,reinit='create_new')

    def part(self,name,metadata=None):
        a=w.Artifact('model','model',metadata=metadata)
        with a.new_file(name) as out:out.write(name)
        return a

    def queued(self,name):
        with self.run_handle(name,True) as run:
            a=self.part(name,{'primary':name});run.finish_artifact(a);run.config['model']=a
        return run,a

    def deliver_parts(self,run):
        journal=Journal(run.sync_dir,self.sink);prefix=[]
        for event in read_journal(run.sync_dir,project_artifacts=False):
            if event['type']=='artifact':break
            prefix.append(event)
        # Models a batch accepted before the sender's acknowledgment was saved.
        self.sink.publish(journal._resolve(prefix))

    def closures(self):
        return [v for v in self.sink._artifact_resolutions.values() if v.get('kind')==KIND]

    def test_hidden_prior_completion_never_reuses_consumed_parts(self):
        with self.run_handle('worker') as run:run.upsert_artifact(self.part('old-worker'))
        with self.run_handle('primary') as run:first=run.finish_artifact(self.part('old-primary'))
        read=self.sink.read_events
        with patch.object(self.sink,'read_events',side_effect=lambda **kw:[e for e in read(**kw) if e['type']!='artifact']):
            with self.run_handle('next') as run:second=run.finish_artifact(self.part('new'))
        self.assertEqual(set(first.manifest.entries),{'old-primary','old-worker'})
        self.assertEqual(set(second.manifest.entries),{'new'})
        records=self.closures();self.assertEqual(len(records),2)
        self.assertTrue(set(records[0]['resolution']['payload']['contribution_ids']).isdisjoint(records[1]['resolution']['payload']['contribution_ids']))

    def test_competing_handles_config_and_raw_retry_identity_converge(self):
        first,a=self.queued('a');second,b=self.queued('b')
        original={r.id:Path(r.sync_dir,'events.jsonl').read_bytes() for r in [first,second]}
        original_b_id=b._id;original_b_part=b._operation_id;entry=b.manifest.entries['b']
        for run in [first,second]:self.deliver_parts(run)
        w.sync(first.sync_dir,sink=self.sink)
        with patch.object(self.sink,'read_events',side_effect=AssertionError('recover from files, not query')):
            w.sync(second.sync_dir,sink=self.sink)
        self.assertEqual(a.state,'COMMITTED');self.assertEqual(b.state,'COMMITTED')
        self.assertNotEqual(original_b_id,a.id);self.assertEqual(a.id,b.id)
        self.assertIs(b.manifest.entries['b'],entry)
        self.assertEqual(b.metadata,{'primary':'a'})
        self.assertEqual({e.birth_artifact_id for e in b.manifest.entries.values()},{a.id})
        api=w.Api._from_store(self.sink,entity='e',project='p')
        for run in [first,second]:
            self.assertEqual(Path(run.sync_dir,'events.jsonl').read_bytes(),original[run.id])
            self.assertEqual(api.run(run.id).config['model']['artifact_id'],a.id)
            self.assertEqual([obj.id for obj in api.run(run.id).logged_artifacts()],[a.id])
            self.assertEqual([obj.id for obj in api.run(run.id).used_artifacts()],[a.id])
        from backprop.artifact_operations import recover_local
        # Reconstructed operations still locate their own original identity.
        self.assertEqual(recover_local(second,original_b_part,'artifact')['payload']['artifact_id'],a.id)

    def test_lost_group_claim_recovers_without_query_and_preserves_late_parts(self):
        run,a=self.queued('primary');claim=self.sink.claim_artifact_resolution
        def lost(key,value):
            result=claim(key,value)
            if value.get('kind')==KIND:raise OSError('lost group claim response')
            return result
        with patch.object(self.sink,'claim_artifact_resolution',side_effect=lost):
            with self.assertRaisesRegex(w.Error,'lost group claim response'):w.sync(run.sync_dir,sink=self.sink)
        self.assertFalse(any(e['type']=='artifact' for e in self.sink.events))
        with self.run_handle('late') as late:late.upsert_artifact(self.part('late'))
        with patch.object(self.sink,'read_events',side_effect=AssertionError('closure must recover')):
            w.sync(run.sync_dir,sink=self.sink)
        self.assertEqual(set(a.manifest.entries),{'primary'})
        with self.run_handle('next') as next_run:b=next_run.finish_artifact(self.part('next'))
        self.assertEqual(set(b.manifest.entries),{'late','next'})

    def test_prepared_remapped_config_is_pending_until_its_envelope_is_acknowledged(self):
        first,a=self.queued('a');second,b=self.queued('b')
        for run in [first,second]:self.deliver_parts(run)
        w.sync(first.sync_dir,sink=self.sink)
        from backprop.deferred_incremental_artifacts import prepare,restored_publication_status
        from backprop.config_artifacts import JournalArtifactStore
        prepare(Journal(second.sync_dir,self.sink),read_journal(second.sync_dir,project_artifacts=False))
        events=read_journal(second.sync_dir);manifest=next(e['payload'] for e in events if e['type']=='artifact')
        restored=w.Artifact._from_manifest(manifest,JournalArtifactStore(second.sync_dir,events,self.sink),'e','p','b')
        self.assertEqual(restored._id,a.id);self.assertFalse(restored_publication_status(restored))
        w.sync(second.sync_dir,sink=self.sink);self.assertTrue(restored_publication_status(restored))

    def test_corrupt_group_record_is_rejected_without_publishing(self):
        first,a=self.queued('first');w.sync(first.sync_dir,sink=self.sink)
        second,b=self.queued('second');record=self.closures()[0];record['scope'][0]='another-entity'
        before=copy.deepcopy(self.sink.events)
        with self.assertRaisesRegex(w.Error,'conflicting scope'):w.sync(second.sync_dir,sink=self.sink)
        self.assertEqual(before,self.sink.events)

    def test_group_isolation(self):
        with self.run_handle('one',group='one') as run:a=run.finish_artifact(self.part('one'))
        with self.run_handle('two',group='two') as run:b=run.finish_artifact(self.part('two'))
        self.assertEqual(set(b.manifest.entries),{'two'})
        self.assertNotEqual(a.id,b.id);self.assertEqual(len(self.closures()),2)

    def test_independent_process_race_crash_and_browser_replay_fixture(self):
        from distributed_artifact_closure_replay_fixture import build
        value=build();self.assertTrue(value['claim_exit_recovered']);self.assertTrue(value['disjoint_generations'])

    def test_pending_descriptive_edits_survive_refresh_and_save(self):
        with self.run_handle('primary') as run:
            a=self.part('primary',{'original':True});publish=self.sink.publish
            def lost(events):
                if any(e['type']=='artifact' for e in events):raise OSError('not acknowledged')
                return publish(events)
            with patch.object(self.sink,'publish',side_effect=lost):
                with self.assertRaises(w.Error):run.finish_artifact(a)
            a.metadata['edited']=True;a.description='changed while pending';a.tags.append('pending-edit')
            run.flush();self.assertEqual(a.state,'COMMITTED')
            self.assertTrue(a.metadata['edited']);self.assertEqual(a.description,'changed while pending')
            self.assertIn('pending-edit',a.tags);version=a.version;a.save()
            found=w.Api._from_store(self.sink,entity='e',project='p').artifact('model:latest')
            self.assertEqual(found.version,version);self.assertTrue(found.metadata['edited'])
            self.assertEqual(found.description,'changed while pending')

    def test_corrupt_private_remapping_cannot_drop_original_files(self):
        run,a=self.queued('primary');w.sync(run.sync_dir,sink=self.sink)
        path=next((Path(run.sync_dir)/'incremental-artifacts').glob('*.json'))
        saved=json.loads(path.read_text());saved['payload']['files']=[]
        from backprop.deferred_incremental_artifacts import _full_payload
        saved['payload']=_full_payload(saved['payload'],{})
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError,'changes an original contribution file'):_=a.state

    def test_table_files_survive_a_canonical_id_change(self):
        first,a=self.queued('a')
        with self.run_handle('b',True) as second:
            b=self.part('b');b.add(w.Table(columns=['x'],data=[[1],[2]]),'table')
            second.finish_artifact(b);second.config['model']=b
        for run in [first,second]:self.deliver_parts(run)
        for run in [first,second]:w.sync(run.sync_dir,sink=self.sink)
        self.assertEqual(b.id,a.id)
        found=w.Api._from_store(self.sink,entity='e',project='p').artifact(b.id)
        self.assertEqual(found.get('table').data,[[1],[2]])

    def test_pending_relog_edits_follow_the_canonical_output(self):
        first,a=self.queued('a')
        with self.run_handle('b',True) as second:
            b=self.part('b');second.finish_artifact(b)
            b.metadata['relogged']=True
            second.log_artifact(b,aliases=['release']);second.config['model']=b
        for run in [first,second]:self.deliver_parts(run)
        for run in [first,second]:w.sync(run.sync_dir,sink=self.sink)
        api=w.Api._from_store(self.sink,entity='e',project='p')
        found=api.artifact('model:release')
        self.assertEqual(found.id,a.id);self.assertTrue(found.metadata['relogged'])
        self.assertEqual([obj.id for obj in api.run('b').used_artifacts()],[a.id])
        update=next(e['payload'] for e in self.sink.events if e['type']=='artifact_update')
        self.assertEqual(update['name'],'model');self.assertEqual(update['artifact_id'],a.id)

    def test_losing_finalizer_advances_without_reusing_a_consumed_path(self):
        first,a=self.queued('a');second,b=self.queued('b')
        # The first snapshot does not yet contain b; the two operations must
        # therefore close separate generations even when old closes are hidden.
        w.sync(first.sync_dir,sink=self.sink)
        read=self.sink.read_events
        with patch.object(self.sink,'read_events',side_effect=lambda **kw:[e for e in read(**kw) if e['type']!='artifact']):
            w.sync(second.sync_dir,sink=self.sink)
        self.assertNotEqual(a.id,b.id);self.assertEqual(set(b.manifest.entries),{'b'})
        with self.run_handle('third') as run:
            # A previously consumed path may hold different bytes in a new round.
            fresh=w.Artifact('model','model')
            with fresh.new_file('a') as out:out.write('different')
            final=run.finish_artifact(fresh)
        self.assertEqual(final.get_entry('a')._read_verified_bytes(),b'different')
        self.assertEqual(set(final.manifest.entries),{'a'})

    def test_new_generation_orders_after_a_fast_prior_clock(self):
        import sys
        from datetime import datetime,timezone
        class FastClock(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2040,1,1,tzinfo=timezone.utc)
        with patch.object(sys.modules['backprop.run'],'datetime',FastClock):
            with self.run_handle('future') as run:first=run.finish_artifact(self.part('first'))
        with self.run_handle('present') as run:second=run.finish_artifact(self.part('second'))
        records=self.closures()
        self.assertGreater(records[1]['resolution']['origin'],records[0]['resolution']['origin'])
        self.assertEqual(w.Api._from_store(self.sink,entity='e',project='p').artifact('model:latest').id,second.id)
        self.assertEqual(set(second.manifest.entries),{'second'})
