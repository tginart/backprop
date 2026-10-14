"""Offline incremental base resolution and durable dependency rewriting.

Architecture: remote base -> offline delta/config references -> faulted sync ->
cached materialization -> independent reads and handoff. Main callers: SDK suite
and release CI. Source journal bytes remain unchanged throughout resolution.
"""
import copy,json,os,shutil,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase, change_directory
from unittest.mock import patch
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS


class DeferredIncrementalArtifacts(ContextTestCase):
    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.directory)}))
        self.sink=w.MemorySink()

    def run_handle(self,identity,offline=False,project='p',reinit='create_new'):
        run=w.init(id=identity,entity='e',project=project,mode='offline' if offline else 'online',dir=str(self.directory),
            _sink=None if offline else self.sink,_deployment={},settings=SETTINGS,reinit=reinit)
        self.addCleanup(run.finish);return run

    def artifact(self,run,name='model',incremental=False,**files):
        artifact=w.Artifact(name,'model',incremental=incremental)
        for filename,data in files.items():
            with artifact.new_file(filename) as output:output.write(data)
        run.log_artifact(artifact);return artifact

    def api(self):return w.Api._from_store(self.sink,entity='e',project='p')

    def test_remote_base_and_config_references_resolve_before_any_upload(self):
        producer=self.run_handle('base');base=self.artifact(producer,keep='old',replace='old');producer.finish()
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,add='new',replace='new')
        offline.config['artifact']=delta;offline.finish()
        original=Path(offline.sync_dir,'events.jsonl').read_bytes();old_version=delta.version
        self.assertEqual(delta.state,'PENDING')
        w.sync(offline.sync_dir,sink=self.sink)
        self.assertEqual(Path(offline.sync_dir,'events.jsonl').read_bytes(),original)
        self.assertEqual(delta.state,'COMMITTED');self.assertNotEqual(delta.version,old_version)
        found=self.api().artifact('model:latest')
        self.assertEqual({key:e._read_verified_bytes() for key,e in found.manifest.entries.items()},
                         {'keep':b'old','replace':b'new','add':b'new'})
        reference=self.api().run('offline').config['artifact']
        self.assertEqual(reference['version'],found.version);self.assertEqual(reference['digest'],found.digest)
        self.assertEqual(reference['artifact_id'],delta.id)
        self.assertFalse(any('_backprop_incremental' in e['payload'] for e in self.sink.events))
        self.assertEqual(found.get_entry('keep').birth_artifact_id,base.id)

    def test_lost_ack_retry_keeps_frozen_base_when_new_remote_version_appears(self):
        run=self.run_handle('base');base=self.artifact(run,keep='old');run.finish()
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,add='new');offline.finish()
        publish=self.sink.publish
        def lost(events):publish(events);raise OSError('ack lost')
        with patch.object(self.sink,'publish',side_effect=lost):
            with self.assertRaises(w.Error):w.sync(offline.sync_dir,sink=self.sink)
        first=[e for e in self.sink.events if e['run_id']=='offline'];self.assertEqual(delta.state,'PENDING')
        later=self.run_handle('later');self.artifact(later,late='must not inherit');later.finish()
        w.sync(offline.sync_dir,sink=self.sink)
        attempts=[e for e in self.sink.events if e['run_id']=='offline'];self.assertEqual(attempts,first+first)
        found=self.api().artifact(delta.id);self.assertEqual(set(found.manifest.entries),{'keep','add'})
        self.assertEqual(found.version,delta.version)

    def test_successive_offline_deltas_inherit_materialized_predecessors(self):
        run=self.run_handle('base');self.artifact(run,base='base');run.finish()
        offline=self.run_handle('offline',True)
        first=self.artifact(offline,incremental=True,first='first')
        second=self.artifact(offline,incremental=True,second='second')
        offline.config.update({'first':first,'second':second});offline.finish()
        w.sync(offline.sync_dir,sink=self.sink)
        a=self.api().artifact(first.id);b=self.api().artifact(second.id)
        self.assertEqual(set(a.manifest.entries),{'base','first'})
        self.assertEqual(set(b.manifest.entries),{'base','first','second'})
        rows=read_journal(offline.sync_dir)
        self.assertTrue(all('_backprop_incremental' not in e['payload'] for e in rows))
        refs=self.api().run('offline').config
        self.assertEqual((refs['first']['version'],refs['second']['version']),(a.version,b.version))

    def test_handoff_resolves_remote_base_without_syncing_private_producer_data(self):
        run=self.run_handle('base');self.artifact(run,base='base');run.finish()
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,delta='delta')
        offline.log({'private':99});offline.finish();original=Path(offline.sync_dir,'events.jsonl').read_bytes()
        reader=self.run_handle('reader',project='inputs')
        self.assertIs(reader.use_artifact(delta),delta);self.assertEqual(delta.state,'COMMITTED')
        self.assertEqual(delta.get_entry('base')._read_verified_bytes(),b'base')
        reader.finish();self.assertEqual(Path(offline.sync_dir,'events.jsonl').read_bytes(),original)
        self.assertEqual(offline._journal.acknowledged_count(),0)
        self.assertFalse(any(e['type']=='history' and e['run_id']=='offline' for e in self.sink.events))
        w.sync(offline.sync_dir,sink=self.sink)
        self.sink.events=list(reversed(self.sink.events))+self.sink.events
        self.assertEqual(set(self.api().artifact(delta.id).manifest.entries),{'base','delta'})

    def test_online_relog_resolves_delta_and_preserves_source_journal(self):
        base=self.run_handle('base');self.artifact(base,base='base');base.finish()
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,delta='delta');offline.finish()
        reader=self.run_handle('reader');reader.log_artifact(delta,aliases=['release']);reader.finish()
        found=self.api().artifact('model:release');self.assertEqual(set(found.manifest.entries),{'base','delta'})
        self.assertEqual(found.get_entry('base')._read_verified_bytes(),b'base')
        self.assertEqual(offline._journal.acknowledged_count(),0)

    def test_lookup_failure_and_corrupt_delta_publish_nothing(self):
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,delta='delta');offline.finish()
        with patch.object(self.sink,'read_events',side_effect=OSError('unavailable')):
            with self.assertRaises(w.Error):w.sync(offline.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.events,[])
        payload=next(e['payload'] for e in read_journal(offline.sync_dir) if e['type']=='artifact')
        Path(offline.sync_dir,payload['files'][0]['_backprop_local_file']).write_text('corrupt')
        with self.assertRaises(w.Error):w.sync(offline.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.events,[])

    def test_independent_process_recovery_and_browser_fixture(self):
        from deferred_incremental_replay_fixture import build
        result=build()
        self.assertTrue(result['recovered_after_hard_exit'])
        self.assertTrue(result['retry_records_identical'])

    def test_two_processes_freeze_one_source_base_for_separate_consumers(self):
        import subprocess,sys
        from artifact_handoff_replay_fixture import DiskSink
        root=Path(__file__).resolve().parents[1]
        remote=self.directory/'remote';self.sink=DiskSink(remote)
        run=self.run_handle('base');self.artifact(run,base='base');run.finish()
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,delta='delta');offline.finish()
        script="""
import sys
import backprop as w
from backprop.config_artifacts import JournalArtifactStore
from backprop.transport import read_journal
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
events=read_journal(sys.argv[1]);event=next(e for e in events if e['type']=='artifact')
a=w.Artifact._from_manifest(event['payload'],JournalArtifactStore(sys.argv[1],events,None),'e','p','offline');a._committed=False
sink=DiskSink(sys.argv[2])
with w.init(id=sys.argv[3],entity='e',project='p',dir=sys.argv[4],_sink=sink,_deployment={},settings=SETTINGS) as run:
 run.use_artifact(a)
 assert set(a.manifest.entries)=={'base','delta'}
"""
        env={**os.environ,'PYTHONPATH':str(root/'sdk')+os.pathsep+str(root/'tests')}
        processes=[subprocess.Popen([sys.executable,'-B','-c',script,offline.sync_dir,str(remote),identity,str(self.directory)],cwd=tempfile.gettempdir(),env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for identity in ['reader1','reader2']]
        for process in processes:
            stdout,stderr=process.communicate(timeout=45);self.assertEqual(process.returncode,0,stderr)
        events=self.sink.read_events();manifests=[e for e in events if e['type']=='artifact' and e['payload']['artifact_id']==delta._id]
        self.assertEqual(len(manifests),2);self.assertEqual(manifests[0],manifests[1])
        self.assertEqual(len(list((Path(offline.sync_dir)/'incremental-artifacts').glob('*.json'))),1)
        self.assertEqual(offline._journal.acknowledged_count(),0)


    def test_restored_handle_observes_sync_and_downloads_full_manifest(self):
        from backprop.config_artifacts import JournalArtifactStore
        base=self.run_handle('base');self.artifact(base,base='base');base.finish()
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,delta='delta');offline.finish()
        events=read_journal(offline.sync_dir);event=next(e for e in events if e['type']=='artifact')
        restored=w.Artifact._from_manifest(event['payload'],JournalArtifactStore(offline.sync_dir,events,self.sink),'e','p','offline')
        entry=restored.get_entry('delta')
        self.assertEqual(restored.state,'PENDING')
        w.sync(offline.sync_dir,sink=self.sink)
        # An explicit root and name access must each refresh independently.
        destination=self.directory/'restored-download'
        restored.download(root=destination)
        self.assertEqual({p.name:p.read_bytes() for p in destination.iterdir()},{'base':b'base','delta':b'delta'})
        self.assertIs(restored.get_entry('delta'),entry)
        self.assertEqual(restored.name,'model:'+self.api().artifact(delta.id).version)
        self.assertEqual(restored.state,'COMMITTED')

    def test_newer_base_clock_and_appended_events_reuse_identical_prefix(self):
        from datetime import datetime,timedelta,timezone
        base=self.run_handle('base');self.artifact(base,base='base');base.finish()
        future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        for event in self.sink.events:
            event['timestamp']=future
            if event['type']=='artifact':event['payload']['created_at']=future
        offline=self.run_handle('offline',True);delta=self.artifact(offline,incremental=True,delta='delta')
        offline.config['model']=delta
        w.sync(offline.sync_dir,sink=self.sink)
        first=[e for e in self.sink.events if e['run_id']=='offline']
        frozen=next(e for e in first if e['type']=='artifact')
        self.assertGreater(datetime.fromisoformat(frozen['timestamp']),datetime.fromisoformat(future))
        offline.log({'after_first_sync':1});offline.finish()
        w.sync(offline.sync_dir,sink=self.sink)
        all_rows=[e for e in self.sink.events if e['run_id']=='offline']
        self.assertEqual(all_rows[:len(first)],first)
        self.assertGreater(datetime.fromisoformat(all_rows[-1]['timestamp']),datetime.fromisoformat(frozen['timestamp']))
        self.assertEqual(self.api().run('offline').state,'finished')
        self.assertEqual(self.api().run('offline').config['model']['version'],delta.version)


    def test_offline_fork_and_increment_resolve_together(self):
        parent=self.run_handle('parent');base=self.artifact(parent,base='base');parent.log({'loss':2});parent.finish()
        child=w.init(id='child',entity='e',project='p',mode='offline',dir=str(self.directory/'child'),
            _deployment={},settings={**SETTINGS,'fork_from':'parent?_step=0'})
        delta=self.artifact(child,incremental=True,delta='delta');child.config['model']=delta;child.log({'loss':1});child.finish()
        original=Path(child.sync_dir,'events.jsonl').read_bytes()
        w.sync(child.sync_dir,sink=self.sink)
        self.assertEqual(Path(child.sync_dir,'events.jsonl').read_bytes(),original)
        found=self.api().artifact(delta.id)
        self.assertEqual(set(found.manifest.entries),{'base','delta'})
        self.assertEqual(self.api().run('child').config['model']['version'],found.version)
        self.assertTrue(all('_backprop_incremental' not in e['payload'] for e in read_journal(child.sync_dir)))

    def test_incremental_input_and_model_use_through_each_public_import(self):
        import importlib,contextlib
        self.enterContext(change_directory(self.directory))
        base=self.run_handle('base');self.artifact(base,base='base');base.finish()
        for operation in ['use_artifact','use_model']:
            for index,namespace in enumerate([w,w.Run,importlib.import_module('wandb.sdk.wandb_run').Run]):
                with self.subTest(operation=operation,index=index):
                    identity=operation+str(index)
                    offline=self.run_handle(identity+'-source',True)
                    delta=self.artifact(offline,incremental=True,delta='delta');offline.finish()
                    consumer=self.run_handle(identity+'-consumer',reinit='finish_previous')
                    call=getattr(namespace,operation)
                    if namespace is w:result=call(delta)
                    else:result=call(consumer,delta)
                    if operation=='use_artifact':self.assertIs(result,delta)
                    else:self.assertEqual({p.name:p.read_bytes() for p in Path(result).iterdir()},{'base':b'base','delta':b'delta'})
                    consumer.finish()
                    found=self.api().artifact(delta.id)
                    self.assertEqual(set(found.manifest.entries),{'base','delta'})
                    self.assertEqual([a.id for a in self.api().run(consumer.id).used_artifacts()],[delta.id])
                    self.assertEqual(offline._journal.acknowledged_count(),0)


    def test_online_resume_inherits_an_unsynced_offline_predecessor(self):
        base=self.run_handle('base');self.artifact(base,base='base');base.finish()
        offline=self.run_handle('offline',True)
        first=self.artifact(offline,incremental=True,first='first');offline.finish()
        resumed=w.init(id='offline',entity='e',project='p',resume='must',dir=str(self.directory),
            _sink=self.sink,_deployment={},settings=SETTINGS,reinit='create_new')
        self.addCleanup(resumed.finish)
        second=self.artifact(resumed,incremental=True,second='second')
        self.assertEqual(set(second.manifest.entries),{'base','first','second'})
        resumed.finish()
        self.assertEqual(set(self.api().artifact(first.id).manifest.entries),{'base','first'})
        self.assertEqual(set(self.api().artifact(second.id).manifest.entries),{'base','first','second'})
