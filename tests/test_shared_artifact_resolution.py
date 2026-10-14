"""Independent journal copies must publish one immutable incremental result.

Architecture: real remote bases -> unresolved offline copies -> competing first
syncs -> shared first-write record -> retry and fresh query. HTTP fixtures model
only the Foundry storage boundary; native journals and replay stay real.
Main callers: the SDK release suite and framework CI.
"""
import copy,io,json,os,shutil,tempfile,threading,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch,MagicMock
from urllib.error import HTTPError
import backprop as w
from backprop.foundry import FoundryStore,FoundryError
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS
from test_backend_transport import CONFIG


class SharedArtifactResolution(ContextTestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.root)}))
        self.sink=w.MemorySink()

    def run_handle(self,identity,offline=False):
        run=w.init(id=identity,entity='e',project='p',mode='offline' if offline else 'online',dir=str(self.root),
            _sink=None if offline else self.sink,_deployment={},settings=SETTINGS,reinit='create_new')
        self.addCleanup(run.finish);return run

    def artifact(self,run,incremental=False,**files):
        artifact=w.Artifact('model','model',incremental=incremental)
        for name,value in files.items():
            with artifact.new_file(name) as target:target.write(value)
        run.log_artifact(artifact);return artifact

    def copies(self):
        base=self.run_handle('base');self.artifact(base,base='base');base.finish()
        source=self.run_handle('source',True);artifact=self.artifact(source,True,delta='delta')
        source.config['model']=artifact;source.finish()
        copies=[]
        for identity in ['copy1','copy2']:
            run=self.run_handle(identity,True);run.log_artifact(artifact);run.config['model']=artifact;run.finish();copies.append(run)
        return source,copies,artifact

    def test_serialized_memory_store_retains_choice_and_recreates_process_lock(self):
        import pickle
        self.sink.claim_artifact_resolution('key',{'winner':1})
        restored=pickle.loads(pickle.dumps(self.sink))
        self.assertEqual(restored.read_artifact_resolution('key'),{'winner':1})
        self.assertEqual(restored.claim_artifact_resolution('key',{'winner':2}),{'winner':1})
        self.assertIsNot(restored._artifact_resolution_lock,self.sink._artifact_resolution_lock)

    def test_changed_remote_base_and_deleted_local_caches_keep_one_version(self):
        source,copies,artifact=self.copies();journals=[source,*copies]
        original={r.id:Path(r.sync_dir,'events.jsonl').read_bytes() for r in journals}
        w.sync(copies[1].sync_dir,sink=self.sink)
        version=w.Api._from_store(self.sink,entity='e',project='p').artifact(artifact._id).version
        later=self.run_handle('later');self.artifact(later,late='late');later.finish()
        with patch.object(self.sink,'read_events',side_effect=AssertionError('Already chosen base must not be queried again')):
            w.sync(source.sync_dir,sink=self.sink);w.sync(copies[0].sync_dir,sink=self.sink)
        self.sink.events=list(reversed(self.sink.events))+self.sink.events
        api=w.Api._from_store(self.sink,entity='e',project='p');found=api.artifact(artifact._id)
        self.assertEqual(found.version,version);self.assertEqual(set(found.manifest.entries),{'base','delta'})
        self.assertEqual(found.get_entry('base')._read_verified_bytes(),b'base')
        for run in journals:
            self.assertEqual(Path(run.sync_dir,'events.jsonl').read_bytes(),original[run.id])
            self.assertEqual(api.run(run.id).config['model']['version'],version)
        # A copied journal can lose every private preparation file and recover
        # from the shared winner even if its stream events are temporarily hidden.
        directory=Path(copies[0].sync_dir)
        shutil.rmtree(directory/'incremental-artifacts');(directory/'ack.json').unlink()
        with patch.object(self.sink,'read_events',return_value=[]):w.sync(directory,sink=self.sink)
        self.assertEqual(w.Api._from_store(self.sink,entity='e',project='p').artifact(artifact._id).version,version)

    def test_lost_claim_response_recovers_without_local_cache_or_source(self):
        source,copies,artifact=self.copies();claim=self.sink.claim_artifact_resolution
        def lost(key,candidate):claim(key,candidate);raise OSError('lost claim response')
        with patch.object(self.sink,'claim_artifact_resolution',side_effect=lost):
            with self.assertRaisesRegex(w.Error,'lost claim response'):w.sync(source.sync_dir,sink=self.sink)
        self.assertFalse(list((Path(source.sync_dir)/'incremental-artifacts').glob('*.json')))
        self.assertFalse(any(e['run_id']=='source' for e in self.sink.events))
        shutil.rmtree(source.sync_dir)
        with patch.object(self.sink,'read_events',side_effect=AssertionError('shared record must recover')):
            w.sync(copies[0].sync_dir,sink=self.sink)
        found=w.Api._from_store(self.sink,entity='e',project='p').artifact(artifact._id)
        self.assertEqual({n:e._read_verified_bytes() for n,e in found.manifest.entries.items()},{'base':b'base','delta':b'delta'})

    def test_conflicting_original_delta_rejects_before_stream_publication(self):
        source,copies,artifact=self.copies();w.sync(source.sync_dir,sink=self.sink)
        path=Path(copies[0].sync_dir,'events.jsonl');events=[json.loads(line) for line in path.read_text().splitlines()]
        for event in events:
            if event['type']=='artifact':event['payload']['metadata']={'changed':True}
        path.write_text(''.join(json.dumps(e)+'\n' for e in events));before=copy.deepcopy(self.sink.events)
        with self.assertRaisesRegex(w.Error,'conflicts with its original delta'):w.sync(copies[0].sync_dir,sink=self.sink)
        self.assertEqual(self.sink.events,before)

    def test_corrupt_shared_record_and_denied_claim_never_publish(self):
        source,copies,artifact=self.copies()
        with patch.object(self.sink,'claim_artifact_resolution',side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(w.Error,'denied'):w.sync(source.sync_dir,sink=self.sink)
        self.assertFalse(any(e['run_id']=='source' for e in self.sink.events))
        w.sync(source.sync_dir,sink=self.sink)
        winner=next(iter(self.sink._artifact_resolutions.values()));winner['payload']['digest']='bad'
        with self.assertRaisesRegex(w.Error,'digest check'):w.sync(copies[0].sync_dir,sink=self.sink)

    def test_corrupt_private_cache_cannot_override_shared_contents(self):
        source,copies,artifact=self.copies();w.sync(source.sync_dir,sink=self.sink)
        path=next((Path(source.sync_dir)/'incremental-artifacts').glob('*.json'))
        value=json.loads(path.read_text());value['payload']['metadata']={'corrupt':True};path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'digest check'):read_journal(source.sync_dir)

    def test_distinct_writer_ordering_survives_shared_manifest(self):
        from backprop.deferred_incremental_artifacts import _project,_identity
        source,copies,artifact=self.copies();w.sync(source.sync_dir,sink=self.sink)
        event=next(json.loads(line) for line in Path(source.sync_dir,'events.jsonl').read_text().splitlines() if json.loads(line)['type']=='artifact')
        winner=copy.deepcopy(next(iter(self.sink._artifact_resolutions.values())))
        winner['payload']['_writer']={'id':'winner','sequence':1}
        event['payload']['_writer']={'id':'this-writer','sequence':77}
        projected=_project([event],{_identity(event):winner})[0]
        self.assertEqual(projected['payload']['_writer'],event['payload']['_writer'])


    def test_different_process_candidates_crash_and_fresh_recovery(self):
        from shared_artifact_resolution_replay_fixture import build
        result=build()
        self.assertTrue(result['different_candidates'])
        self.assertTrue(result['recovered_after_claim_exit'])


class FoundryArtifactResolutionHTTP(ContextTestCase):
    def setUp(self):self.store=FoundryStore(CONFIG,token='test-token');self.key='a'*64

    def response(self,value):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps(value).encode();response.headers={'Content-Type':'application/json'}
        return response

    def error(self,name,status=404):
        return HTTPError('https://foundry.example.com/file',status,'expected',{},io.BytesIO(json.dumps({'errorName':name}).encode()))

    def test_absent_is_distinct_from_permissions_branch_and_bad_json(self):
        with patch.object(self.store.opener,'open',side_effect=self.error('FileNotFoundOnBranch')):
            self.assertIsNone(self.store.read_artifact_resolution(self.key))
        for name,status in [('DatasetNotFound',404),('BranchNotFound',404),('PermissionDenied',403),('UnknownError',404)]:
            with patch.object(self.store.opener,'open',side_effect=self.error(name,status)):
                with self.assertRaises(FoundryError):self.store.read_artifact_resolution(self.key)
        with patch.object(self.store.opener,'open',return_value=self.response([])):
            with self.assertRaises(ValueError):self.store.read_artifact_resolution(self.key)
        with patch.object(self.store,'request') as request:
            for key in ['../escape','A'*64,'a'*63,None]:
                with self.assertRaises(ValueError):self.store.read_artifact_resolution(key)
            request.assert_not_called()

    def test_append_winner_duplicate_and_lost_response(self):
        winner={'chosen':1};candidate={'chosen':2}
        with patch.object(self.store.opener,'open',side_effect=[self.error('FileAlreadyExists'),self.response(winner)]) as opened:
            self.assertEqual(self.store.claim_artifact_resolution(self.key,candidate),winner)
        write=opened.call_args_list[0].args[0]
        self.assertIn('transactionType=APPEND',write.full_url)
        self.assertIn('artifact-resolutions%2F',write.full_url)
        self.assertEqual(json.loads(write.data),candidate)
        from urllib.error import URLError
        with patch.object(self.store.opener,'open',side_effect=[URLError(TimeoutError()),self.error('FileAlreadyExists'),self.response(winner)]),patch('backprop.foundry.time.sleep'):
            self.assertEqual(self.store.claim_artifact_resolution(self.key,candidate),winner)

    def test_only_known_append_contention_retries(self):
        with patch.object(self.store.opener,'open',side_effect=[self.error('OpenTransactionAlreadyExists',409),self.response({})]) as opened,patch('backprop.foundry.time.sleep'):
            self.assertEqual(self.store.claim_artifact_resolution(self.key,{'chosen':1}),{'chosen':1})
            self.assertEqual(opened.call_count,2)
        for name,status in [('PermissionDenied',403),('UnknownConflict',409),('DatasetNotFound',404)]:
            with patch.object(self.store.opener,'open',side_effect=self.error(name,status)) as opened:
                with self.assertRaises(FoundryError):self.store.claim_artifact_resolution(self.key,{})
                self.assertEqual(opened.call_count,1)
