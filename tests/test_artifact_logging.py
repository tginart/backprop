"""Public publication contracts plus durable immutable output behavior.

Architecture: pinned subprocess comparison -> real file journals -> independent
queries and recovery. Main callers: SDK suite and release CI. Hosted reference
boundaries are explicit; native persistence is never mocked out of these tests.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactLoggingContracts(ContextTestCase):
    def test_pinned_public_arguments_defaults_errors_lifecycle_and_aliases(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');records=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_logging_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=180,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True);(folder/f'artifact-logging-{mode}.json').write_text(result.stdout)
        for label,group in records[0].items():
            if label=='source_hashes':continue
            for key,expected in group.items():
                actual=records[1][label][key]
                if label=='wandb.log_artifact' and key=='signature':
                    self.assertEqual(expected[0],['self','POSITIONAL_OR_KEYWORD','required'])
                    self.assertEqual(expected[1:],actual)
                elif label in ['wandb.log_artifact','wandb.log_model'] and key.startswith('finished/'):
                    self.assertEqual(expected['error'],'UsageError')
                    self.assertEqual(actual,{'error':'Error','message':f"You must call wandb.init() before {label}()"})
                elif label=='wandb.log_model' and key.startswith('disabled/'):
                    # Pinned module export retains the previous finished binding.
                    self.assertEqual(expected['error'],'UsageError');self.assertEqual(actual,{'return':'None'})
                else:self.assertEqual(expected,actual,(label,key))

    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.directory)}))
        self.sink=w.MemorySink()

    def run_handle(self,identity,*,offline=False,**kwargs):
        run=w.init(id=identity,entity='e',project='p',mode='offline' if offline else 'online',dir=str(self.directory),
            _sink=None if offline else self.sink,_deployment={},settings=SETTINGS,**kwargs)
        self.addCleanup(run.finish);return run

    def test_path_defaults_and_incremental_snapshots_publish_real_bytes(self):
        path=self.directory/'weights.bin';path.write_bytes(b'weights')
        run=self.run_handle('writer')
        artifact=w.log_artifact(path)
        self.assertEqual(artifact.type,'unspecified')
        self.assertEqual(artifact._name,'run-writer-weights.bin')
        self.assertIsNone(w.log_model(path,name='model',aliases=['best']))
        part=w.Artifact('part','data',incremental=True);part.add_file(str(path))
        self.assertIs(run.log_artifact(part),part);run.finish()
        api=w.Api._from_store(self.sink,entity='e',project='p')
        self.assertEqual(api.artifact('model:best').get_entry('weights.bin')._read_verified_bytes(),b'weights')
        self.assertEqual(len(list(api.run('writer').logged_artifacts())),3)
        self.assertFalse(any(e['type']=='artifact_part' for e in self.sink.events))

    def test_relogging_preserves_contents_and_updates_edit_baseline(self):
        run=self.run_handle('writer');artifact=w.Artifact('model','data',metadata={'original':1})
        with artifact.new_file('data') as output:output.write('immutable')
        run.log_artifact(artifact,aliases=['best']);run.flush();identity,version=artifact.id,artifact.version
        artifact.metadata={'edited':2};artifact.description='relogged'
        self.assertIs(run.log_artifact(artifact,aliases=['release'],tags=['trained']),artifact);run.flush()
        artifact.aliases.remove('release');artifact.save()
        api=w.Api._from_store(self.sink,entity='e',project='p')
        with self.assertRaises(w.CommError):api.artifact('model:release')
        found=api.artifact('model:best')
        self.assertEqual((found.id,found.version),(identity,version));self.assertEqual(found.metadata,{'edited':2})
        self.assertEqual(found.description,'relogged');self.assertEqual(found.tags,['trained'])
        self.assertEqual(found.get_entry('data')._read_verified_bytes(),b'immutable')
        self.assertEqual(len(list(api.run('writer').logged_artifacts())),1)

    def test_offline_source_relogging_copies_snapshot_and_survives_lost_ack(self):
        import shutil
        source=self.run_handle('source',offline=True);artifact=w.Artifact('model','data')
        with artifact.new_file('data') as output:output.write('offline bytes')
        source.log_artifact(artifact);source.log({'private':1});source.finish()
        source_bytes=Path(source.sync_dir,'events.jsonl').read_bytes()
        run=self.run_handle('consumer');run.log_artifact(artifact,aliases=['release'])
        self.assertEqual(Path(source.sync_dir,'events.jsonl').read_bytes(),source_bytes)
        shutil.rmtree(source.sync_dir)
        publish=self.sink.publish
        def lost(events):publish(events);raise OSError('lost acknowledgement')
        with patch.object(self.sink,'publish',side_effect=lost):
            with self.assertRaises(w.Error):run.flush()
        self.assertEqual(artifact.state,'PENDING')
        artifact.wait();run.finish()
        self.assertEqual(artifact.state,'COMMITTED')
        self.sink.events=list(reversed(self.sink.events))+self.sink.events
        api=w.Api._from_store(self.sink,entity='e',project='p')
        found=api.artifact('model:release');self.assertEqual(found.get_entry('data')._read_verified_bytes(),b'offline bytes')
        self.assertEqual(len(list(api.run('consumer').logged_artifacts())),1)
        self.assertFalse(any(e['run_id']=='source' for e in self.sink.events))

    def test_preflight_conflicts_and_invalid_tags_cannot_publish(self):
        run=self.run_handle('writer');run.log_artifact(w.Artifact('model','data'));run.flush()
        before=len(read_journal(run.sync_dir));wrong=w.Artifact('model','model')
        with self.assertRaisesRegex(ValueError,'already exists with type'):
            run.log_artifact(wrong)
        self.assertEqual(len(read_journal(run.sync_dir)),before)
        with self.assertRaisesRegex(ValueError,'Invalid tag'):
            run.log_artifact('/missing/path',tags=['bad/tag'])
        self.assertEqual(len(read_journal(run.sync_dir)),before)

    def test_independent_shared_writers_can_relog_same_immutable_artifact(self):
        run=self.run_handle('producer');artifact=w.Artifact('model','data');run.log_artifact(artifact);run.finish()
        for label,primary in [('primary',True),('secondary',False)]:
            found=w.Api._from_store(self.sink,entity='e',project='p').artifact('model:latest')
            with w.init(id='shared',entity='e',project='p',mode='shared',dir=str(self.directory),_sink=self.sink,_deployment={},
                settings={**SETTINGS,'x_label':label,'x_primary':primary}) as shared:
                shared.log_artifact(found,aliases=[label])
        api=w.Api._from_store(self.sink,entity='e',project='p')
        self.assertEqual(len(list(api.run('shared').logged_artifacts())),1)
        self.assertEqual(api.artifact('model:secondary').id,artifact.id)

    def test_incremental_inherits_previous_files_overlays_conflicts_and_retries_same_base(self):
        for offline in [False,True]:
            with self.subTest(offline=offline):
                run=self.run_handle('incremental-'+str(offline),offline=offline)
                first=w.Artifact('collection-'+str(offline),'data')
                for name,value in [('kept','original'),('replaced','old')]:
                    with first.new_file(name) as out:out.write(value)
                run.log_artifact(first)
                if not offline:run.flush()
                second=w.Artifact(first._name,'data',incremental=True)
                for name,value in [('added','new'),('replaced','newer')]:
                    with second.new_file(name) as out:out.write(value)
                append=run._journal.append
                with patch.object(run._journal,'append',side_effect=OSError('journal unavailable')):
                    with self.assertRaises(OSError):run.log_artifact(second)
                if offline:self.assertTrue(second._incremental_pending)
                else:self.assertEqual(second._incremental_base_id,first._id)
                run.log_artifact(second);run.finish()
                if offline:w.sync(run.sync_dir,sink=self.sink)
                found=w.Api._from_store(self.sink,entity='e',project='p').artifact(first._name+':latest')
                self.assertEqual({name:entry._read_verified_bytes() for name,entry in found.manifest.entries.items()},
                    {'kept':b'original','added':b'new','replaced':b'newer'})
                self.assertEqual(found.get_entry('kept').birth_artifact_id,first._id)
                self.assertEqual(found.get_entry('replaced').birth_artifact_id,second._id)

    def test_hard_exit_restart_delayed_source_and_shared_output_replay(self):
        from artifact_logging_replay_fixture import build
        result=build()
        self.assertTrue(result['recovered_after_hard_exit'])
        self.assertTrue(result['file_bytes_verified'])

    def test_use_artifact_prepares_incremental_base_through_same_publication_path(self):
        run=self.run_handle('reader');first=w.Artifact('model','model')
        with first.new_file('base') as output:output.write('base')
        run.log_artifact(first);run.flush()
        second=w.Artifact('model','model',incremental=True)
        with second.new_file('next') as output:output.write('next')
        self.assertIs(run.use_artifact(second),second)
        self.assertEqual(set(second.manifest.entries),{'base','next'})
        run.finish();api=w.Api._from_store(self.sink,entity='e',project='p')
        used=list(api.run('reader').used_artifacts());self.assertEqual([a.id for a in used],[second.id])
        self.assertEqual(used[0].get_entry('base')._read_verified_bytes(),b'base')
