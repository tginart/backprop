"""Artifact output ownership must agree with its durable publication journal.

Architecture: pinned public/transport comparison -> pending and committed copies
-> lost acknowledgments and independent replay -> SDK/browser observations.
Main callers: SDK and CI suites. No hosted W&B behavior is simulated as evidence.
"""
import importlib.metadata,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactNamespaceContracts(ContextTestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.folder)}))
        self.sink=w.MemorySink()

    def run_handle(self,identity,project='p',entity='e',offline=False):
        run=w.init(id=identity,entity=entity,project=project,mode='offline' if offline else 'online',dir=str(self.folder),
            _sink=None if offline else self.sink,_deployment={},settings=SETTINGS,reinit='create_new')
        self.addCleanup(run.finish);return run

    def artifact(self,run,incremental=False):
        artifact=w.Artifact('model','model',incremental=incremental)
        with artifact.new_file('weights') as out:out.write('immutable')
        run.log_artifact(artifact,aliases=['source']);return artifact

    def test_pinned_public_aliases_serialize_destination_scope(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_namespace_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=120,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True);(folder/f'artifact-namespace-{mode}.json').write_text(result.stdout)
        for key,expected in observations[0].items():
            if key=='source_hashes':continue
            actual=observations[1][key]
            self.assertEqual(actual,expected,key)


    def test_pending_copies_move_scope_producer_and_retry_ownership_together(self):
        for incremental in [False,True]:
          for entity,project in [('e','p'),('e','q'),('other','p')]:
            with self.subTest(incremental=incremental,entity=entity,project=project):
                self.folder=self.folder/f'{incremental}-{entity}-{project}'
                source=self.run_handle('source',offline=True);artifact=self.artifact(source,incremental);source.finish()
                original=Path(source.sync_dir,'events.jsonl').read_bytes()
                copy=self.run_handle('copy',project,entity,True);copy.log_artifact(artifact,aliases=['release']);copy.config['model']=artifact;copy.finish()
                self.assertEqual((artifact.entity,artifact.project,artifact._run_id),(entity,project,'copy'))
                self.assertIs(artifact._owner,copy)
                self.assertEqual(Path(source.sync_dir,'events.jsonl').read_bytes(),original)
                shutil.rmtree(source.sync_dir)
                consumer=self.run_handle('reader',project,entity)
                publish=self.sink.publish
                def lost(events):publish(events);raise OSError('lost acknowledgement')
                with patch.object(self.sink,'publish',side_effect=lost):
                    with self.assertRaises(w.Error):consumer.use_artifact(artifact)
                self.assertIs(consumer.use_artifact(artifact),artifact)
                consumer.finish();w.sync(copy.sync_dir,sink=self.sink)
                api=w.Api._from_store(self.sink,entity=entity,project=project);found=api.artifact('model:release')
                self.assertEqual(found.get_entry('weights')._read_verified_bytes(),b'immutable')
                self.assertEqual([a.id for a in api.run('reader').used_artifacts()],[found.id])
                self.assertEqual(api.run('copy').config['model']['source_project'],project)
                self.assertEqual(found.logged_by().id,'copy')
                self.sink=w.MemorySink()

    def test_committed_offline_copy_preserves_source_edits_and_scoped_aliases(self):
        source=self.run_handle('source');artifact=self.artifact(source);source.finish()
        identity=(artifact.entity,artifact.project,artifact._run_id,artifact.id,artifact.version)
        aliases=list(artifact.aliases);baseline=list(artifact._saved_aliases)
        copy=self.run_handle('copy','q',offline=True);copy.log_artifact(artifact,aliases=['release'],tags=['copied']);copy.finish()
        self.assertEqual((artifact.entity,artifact.project,artifact._run_id,artifact.id,artifact.version),identity)
        self.assertEqual(artifact.aliases,aliases);self.assertEqual(artifact._saved_aliases,baseline);self.assertEqual(artifact.tags,[])
        w.sync(copy.sync_dir,sink=self.sink)
        artifact.tags=['source-edit'];artifact.save()
        api=w.Api._from_store(self.sink,entity='e',project='p')
        self.assertEqual(api.artifact('model:source').tags,['source-edit'])
        other=api.artifact('e/q/model:release');self.assertEqual(other.tags,['copied'])
        self.assertEqual(other.aliases,['latest','release'])
        self.assertEqual(other.get_entry('weights')._read_verified_bytes(),b'immutable')
        with self.assertRaises(w.CommError):api.artifact('e/q/model:source')

    def test_pending_online_copy_chooses_destination_incremental_base(self):
        for project in ['p','q']:
            base_run=self.run_handle('base-'+project,project)
            base=w.Artifact('model','model')
            with base.new_file(project) as out:out.write(project)
            base_run.log_artifact(base);base_run.finish()
        source=self.run_handle('source',offline=True);artifact=self.artifact(source,True);source.finish()
        copy=self.run_handle('copy','q');self.assertIs(copy.log_artifact(artifact,aliases=['release']),artifact)
        copy.finish()
        api=w.Api._from_store(self.sink,entity='e',project='q');found=api.artifact('model:release')
        self.assertEqual(set(found.manifest.entries),{'q','weights'})
        self.assertEqual(artifact.project,'q');self.assertEqual(artifact.state,'COMMITTED')
        self.assertEqual(artifact.version,found.version)
        w.sync(source.sync_dir,sink=self.sink)
        self.assertEqual(set(api.artifact('e/p/model:source').manifest.entries),{'p','weights'})

    def test_independent_committed_copies_share_destination_identity_not_source_aliases(self):
        source=self.run_handle('source');original=self.artifact(source);source.finish()
        api=w.Api._from_store(self.sink,entity='e',project='p')
        copies=[]
        for identity,alias in [('copy1','first'),('copy2','second')]:
            artifact=api.artifact('model:source')
            copy=self.run_handle(identity,'q',offline=True);copy.log_artifact(artifact,aliases=[alias]);copy.finish();copies.append(copy)
        for copy in reversed(copies):w.sync(copy.sync_dir,sink=self.sink)
        self.sink.events=list(reversed(self.sink.events))+self.sink.events
        api=w.Api._from_store(self.sink,entity='e',project='q');found=api.artifact('model:second')
        self.assertNotEqual(found.id,original.id)
        self.assertEqual(api.artifact('model:first').id,found.id)
        self.assertEqual(found.aliases,['first','latest','second'])
        for copy in copies:self.assertEqual([a.id for a in api.run(copy.id).logged_artifacts()],[found.id])
        self.assertEqual(found.get_entry('weights')._read_verified_bytes(),b'immutable')

    def test_pending_copy_can_return_to_original_project_without_identity_conflict(self):
        source=self.run_handle('source',offline=True);artifact=self.artifact(source,True);source.finish();original=artifact._id
        copy=self.run_handle('copy','q',offline=True);copy.log_artifact(artifact,aliases=['away']);copy.finish();away=artifact._id
        back=self.run_handle('back','p',offline=True);back.log_artifact(artifact,aliases=['returned']);back.finish()
        self.assertEqual(len({original,away,artifact._id}),3)
        for run in [back,source,copy]:w.sync(run.sync_dir,sink=self.sink)
        api=w.Api._from_store(self.sink,entity='e',project='p')
        self.assertEqual(api.artifact('model:source').id,original)
        self.assertEqual(api.artifact('model:returned').id,artifact._id)
        self.assertEqual(api.artifact('e/q/model:away').id,away)

    def test_failed_append_does_not_reassign_pending_handle(self):
        source=self.run_handle('source',offline=True);artifact=self.artifact(source);source.finish()
        copy=self.run_handle('copy','q',offline=True)
        with patch.object(copy._journal,'append',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):copy.log_artifact(artifact)
        self.assertIs(artifact._owner,source);self.assertEqual(artifact.project,'p')
        copy.log_artifact(artifact);self.assertEqual(artifact.project,'q')

    def test_independent_process_recovery_and_browser_fixture(self):
        from artifact_namespace_replay_fixture import build
        result=build();self.assertTrue(result['recovered_after_hard_exit']);self.assertTrue(result['file_bytes_verified'])
