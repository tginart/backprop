"""Save complete draft lifecycles and edits through ordinary durable journals.

Architecture: pinned routing comparison -> real active/automatic runs -> files,
acknowledgment and restart/replay. Main callers: SDK release and CI suites. Only
network credentials are injected for native automatic runs; save/init stay real.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactSaveContracts(ContextTestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.root)}));self.sink=w.MemorySink()

    def run_handle(self,name,project='p',offline=False,reinit='default'):
        run=w.init(id=name,entity='e',project=project,mode='offline' if offline else 'online',dir=str(self.root),reinit=reinit,
            _sink=None if offline else self.sink,_deployment={},settings=SETTINGS)
        self.addCleanup(run.finish);return run

    def draft(self,incremental=False):
        artifact=w.Artifact('model','model',incremental=incremental)
        with artifact.new_file('weights') as out:out.write('immutable')
        return artifact

    def test_pinned_signatures_aliases_routing_defaults_and_errors(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');records=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_save_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=90,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                directory=ROOT/'test-results';directory.mkdir(exist_ok=True);(directory/f'artifact-save-{mode}.json').write_text(result.stdout)
        for module,expected in records[0].items():
            if module=='source_hashes':continue
            self.assertEqual(len(expected)-1,93)
            for key,value in expected.items():self.assertEqual(records[1][module][key],value,(module,key))

    def test_latest_active_run_wins_even_when_module_global_is_older(self):
        first=self.run_handle('first','first');second=self.run_handle('second','second',reinit='create_new')
        self.assertIs(w.run,first);artifact=self.draft()
        self.assertIsNone(artifact.save(project='ignored',settings=object()))
        self.assertIs(artifact._owner,second);self.assertEqual(artifact.project,'second')
        second.finish();other=self.draft();other.save();self.assertIs(other._owner,first)
        first.finish()
        self.assertEqual(len(w.Api._from_store(self.sink,entity='e',project='second').run('second').logged_artifacts()),1)

    def test_real_disabled_global_does_not_swallow_artifact_publication(self):
        try: reference = importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError: self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version, '0.30.0')
        results = []
        for mode in ('reference', 'native'):
            environment = {k: v for k, v in os.environ.items() if not k.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            environment['PYTHONPATH'] = str(reference.locate_file('')) if mode == 'reference' else str(ROOT/'sdk')
            result = subprocess.run([sys.executable, '-B', str(ROOT/'tests/artifact_disabled_save_probe.py'), mode],
                cwd=tempfile.gettempdir(), env=environment, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            results.append(json.loads(result.stdout))
        self.assertEqual(*results)
        self.assertEqual(results[1]['False']['automatic_runs'], 1)
        self.assertEqual(results[1]['True']['active_publications'], 1)
        self.assertTrue(all(row['disabled_publications'] == 0 for row in results[1].values()))

    def test_queued_offline_save_creates_and_finishes_automatic_run_with_requested_settings(self):
        source=self.run_handle('source',offline=True);artifact=self.draft(True);source.log_artifact(artifact);source.finish()
        original=Path(source.sync_dir,'events.jsonl').read_bytes();old_id=artifact._id
        self.assertIsNone(artifact.save(project='destination',settings=w.Settings(**SETTINGS,mode='offline',run_id='automatic')))
        automatic=artifact._owner
        self.assertEqual((automatic.id,automatic.project,automatic.job_type),('automatic','destination','auto'))
        self.assertTrue(automatic._closed);self.assertIsNone(w.run)
        self.assertEqual(artifact.state,'PENDING');self.assertNotEqual(artifact._id,old_id)
        self.assertEqual(Path(source.sync_dir,'events.jsonl').read_bytes(),original)
        w.sync(automatic.sync_dir,sink=self.sink)
        found=w.Api._from_store(self.sink,entity='e',project='destination').artifact('model:latest')
        self.assertEqual(found.get_entry('weights')._read_verified_bytes(),b'immutable')
        self.assertEqual(artifact.state,'COMMITTED')

    def test_pending_online_save_relogs_but_committed_save_only_edits(self):
        run=self.run_handle('writer');artifact=self.draft();run.log_artifact(artifact)
        before=sum(e['type']=='artifact' for e in read_journal(run.sync_dir))
        artifact.save(project='ignored',settings=object())
        self.assertEqual(sum(e['type']=='artifact' for e in read_journal(run.sync_dir)),before+1)
        run.flush();identity,version=artifact.id,artifact.version
        artifact.metadata={'edited':True};artifact.save(project='ignored',settings=object())
        self.assertEqual(sum(e['type']=='artifact' for e in read_journal(run.sync_dir)),before+1)
        found=w.Api._from_store(self.sink,entity='e',project='p').artifact(identity)
        self.assertEqual((found.id,found.version),(identity,version));self.assertEqual(found.metadata,{'edited':True})

    def test_disabled_automatic_run_does_not_publish_or_commit_draft(self):
        artifact=self.draft();self.assertIsNone(artifact.save(settings=w.Settings(mode='disabled')))
        self.assertEqual(artifact.state,'PENDING');self.assertFalse(artifact._logged);self.assertIsNone(w.run)


    def test_automatic_default_is_silent_and_init_updates_supplied_settings(self):
        original_init=w.init;created=[];calls=[]
        def initialize(**kwargs):
            calls.append(kwargs)
            run=original_init(**kwargs,dir=str(self.root),_sink=None if kwargs['settings'].mode=='offline' else self.sink,_deployment={})
            created.append(run);return run
        artifact=self.draft()
        with patch.object(w,'init',side_effect=initialize):artifact.save(project='automatic')
        self.assertTrue(calls[0]['settings'].silent);self.assertTrue(created[0]._closed)
        self.assertEqual(created[0].job_type,'auto');self.assertEqual(artifact.state,'COMMITTED')
        settings=w.Settings(**SETTINGS,mode='offline',project='setting-project',entity='setting-entity',run_id='configured')
        before=settings.model_dump();other=self.draft()
        with patch.object(w,'init',side_effect=initialize):other.save(project='',settings=settings)
        # Artifact.save passes the original Settings to init, whose explicit
        # arguments mutate that object in pinned W&B. The test's local directory
        # override and save's auto job type are the only changed fields.
        before.update(root_dir=str(self.root),run_job_type='auto')
        self.assertEqual(settings.model_dump(),before);self.assertIs(calls[-1]['settings'],settings)
        self.assertEqual((created[-1].entity,created[-1].project),('setting-entity','setting-project'))
        self.assertTrue(created[-1]._closed);self.assertEqual(other.state,'PENDING')

    def test_automatic_save_failure_retains_durable_journal_for_sync(self):
        original_init=w.init;created=[];publish=self.sink.publish
        def initialize(**kwargs):
            run=original_init(**kwargs,dir=str(self.root),_sink=self.sink,_deployment={});created.append(run);return run
        def lost(events):
            publish(events)
            if any(e['type']=='artifact' for e in events):raise OSError('lost acknowledgement')
        artifact=self.draft()
        with patch.object(w,'init',side_effect=initialize),patch.object(self.sink,'publish',side_effect=lost):
            with self.assertRaises(w.Error):artifact.save(project='automatic',settings=w.Settings(**SETTINGS,finish_timeout_raises=True))
        self.assertTrue(created[0]._closed);self.assertIsNone(w.run);self.assertEqual(artifact.state,'PENDING')
        w.sync(created[0].sync_dir,sink=self.sink)
        self.sink.events=list(reversed(self.sink.events))+self.sink.events
        found=w.Api._from_store(self.sink,entity='',project='automatic').artifact('model:latest')
        self.assertEqual(found.get_entry('weights')._read_verified_bytes(),b'immutable')
        self.assertEqual(artifact.state,'COMMITTED')

    def test_restored_empty_offline_config_is_a_draft_before_save(self):
        from backprop.config_artifacts import hydrate_config
        source=self.run_handle('empty',offline=True);artifact=w.Artifact('empty-model','model');source.log_artifact(artifact)
        source.config['model']=artifact;source.finish()
        from types import SimpleNamespace
        restored=hydrate_config(SimpleNamespace(config=SimpleNamespace(_data={'model':artifact._reference_json()}),entity='e',project='p',_sink=None),read_journal(source.sync_dir),source.sync_dir)['model']
        self.assertEqual(restored.state,'PENDING')
        restored.save(project='saved-empty',settings=w.Settings(**SETTINGS,mode='offline',run_id='save-empty'))
        self.assertEqual(restored._owner.project,'saved-empty')
        w.sync(restored._owner.sync_dir,sink=self.sink);self.assertEqual(restored.state,'COMMITTED')

    def test_hard_exit_recovery_and_independent_edits(self):
        from artifact_save_replay_fixture import build
        result=build()
        self.assertTrue(result['hard_exit_recovery']);self.assertTrue(result['file_bytes_verified'])
        self.assertTrue(result['empty_manifest_verified']);self.assertTrue(result['source_removed'])

    def test_embedded_table_artifact_acknowledges_containing_event(self):
        from backprop.artifact_events import iter_artifact_events
        from backprop.config_artifacts import hydrate_config
        from types import SimpleNamespace
        run=self.run_handle('table',offline=True)
        run.log({'table':w.Table(columns=['x'],data=[[1]])});run.finish()
        events=read_journal(run.sync_dir);event=next(iter_artifact_events(events))
        reference={'_type':'artifact','artifact_id':event['payload']['artifact_id'],'source_entity':'e','source_project':'p'}
        holder=SimpleNamespace(config=SimpleNamespace(_data={'table':reference}),entity='e',project='p',_sink=None)
        restored=hydrate_config(holder,events,run.sync_dir)['table']
        self.assertEqual(restored.state,'PENDING')
        w.sync(run.sync_dir,sink=self.sink)
        self.assertEqual(restored.state,'COMMITTED')
        self.assertEqual(hydrate_config(holder,events,run.sync_dir)['table'].state,'COMMITTED')

    def test_prepared_branch_uses_its_own_acknowledgment(self):
        from backprop.config_artifacts import hydrate_config
        from types import SimpleNamespace
        parent=self.run_handle('parent');parent.log({'x':1});parent.finish()
        child=w.init(id='child',entity='e',project='p',mode='offline',dir=str(self.root/'branch'),_deployment={},
            settings={**SETTINGS,'fork_from':'parent?_step=0'})
        artifact=w.Artifact('branch-empty','model');child.log_artifact(artifact);child.config['model']=artifact;child.finish()
        holder=SimpleNamespace(config=SimpleNamespace(_data={'model':artifact._reference_json()}),entity='e',project='p',_sink=None)
        publish=self.sink.publish
        def lost(events):
            publish(events)
            if any(event['type']=='artifact' for event in events):raise OSError('lost acknowledgment')
        with patch.object(self.sink,'publish',side_effect=lost):
            with self.assertRaisesRegex(w.Error,'lost acknowledgment'):w.sync(child.sync_dir,sink=self.sink)
        self.assertTrue(Path(child.sync_dir,'branch-events.jsonl').exists())
        restored=hydrate_config(holder,read_journal(child.sync_dir),child.sync_dir)['model']
        self.assertEqual(restored.state,'PENDING')
        w.sync(child.sync_dir,sink=self.sink)
        self.assertEqual(restored.state,'COMMITTED')
        self.assertEqual(hydrate_config(holder,read_journal(child.sync_dir),child.sync_dir)['model'].state,'COMMITTED')
