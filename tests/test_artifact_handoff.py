"""Durable transfer of offline artifact snapshots without unrelated uploads.

Architecture: real offline producer -> selected transfer journal -> injected
acknowledgement/restart failures -> independent artifact/run reads. Main callers:
SDK suite and CI. The source journal must remain byte-identical and unacknowledged.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from runtime_test_case import ContextTestCase
from unittest.mock import patch

import backprop as w
from backprop.config_artifacts import JournalArtifactStore
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS

ROOT = Path(__file__).resolve().parents[1]


class OfflineArtifactHandoff(ContextTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {'BACKPROP_DIR': str(self.directory)}))
        self.sink = w.MemorySink()

    def producer(self, *, empty=False):
        with w.init(id='offline-owner', entity='e', project='source', mode='offline',
                    dir=str(self.directory), _deployment={}, settings=SETTINGS) as run:
            artifact = w.Artifact('weights', 'MODEL', metadata={'revision': 1})
            if not empty:
                with artifact.new_file('nested/weights.bin', mode='wb') as output:
                    output.write(b'frozen offline model')
            run.log_artifact(artifact, aliases=['best'])
            run.log({'private_metric': 7})
            unrelated = w.Artifact('unrelated', 'data')
            with unrelated.new_file('private.bin') as output:
                output.write('unrelated file')
            run.log_artifact(unrelated)
        return artifact, run

    def consumer(self, identity='reader', *, global_run=False):
        run = w.init(id=identity, entity='e', project='consumer', dir=str(self.directory),
                     _sink=self.sink, _deployment={}, settings=SETTINGS, reinit='return_previous' if global_run else 'create_new')
        self.addCleanup(run.finish)
        return run

    def test_only_selected_snapshot_uploads_and_later_source_sync_deduplicates(self):
        artifact, owner = self.producer()
        original = Path(owner.sync_dir, 'events.jsonl').read_bytes()
        version, identity, entry = artifact.version, artifact._id, artifact.get_entry('nested/weights.bin')
        reader = self.consumer()
        self.assertIs(reader.use_artifact(artifact, aliases=['release']), artifact)
        self.assertEqual(artifact.state, 'COMMITTED')
        self.assertEqual(artifact.version, version)
        self.assertEqual(artifact._run_id, owner.id)
        self.assertIs(artifact.get_entry('nested/weights.bin'), entry)
        self.assertEqual(entry._read_verified_bytes(), b'frozen offline model')
        source_events = [event for event in self.sink.events if event['run_id'] == owner.id]
        self.assertEqual([event['type'] for event in source_events], ['artifact', 'artifact_update'])
        self.assertIn(b'frozen offline model', self.sink.files.values())
        self.assertNotIn(b'unrelated file', self.sink.files.values())
        self.assertEqual(Path(owner.sync_dir, 'events.jsonl').read_bytes(), original)
        self.assertEqual(owner._journal.acknowledged_count(), 0)
        transfer = read_journal(artifact._handoff_journal)
        selected = next(e for e in read_journal(owner.sync_dir) if e['type']=='artifact' and e['payload']['artifact_id']==identity)
        self.assertEqual(transfer, [selected])
        reader.finish()
        w.sync(owner.sync_dir, sink=self.sink)
        events = copy.deepcopy(self.sink.events)
        self.sink.events = list(reversed(events)) + events
        api = w.Api._from_store(self.sink, entity='e', project='source')
        self.assertEqual(api.artifact('weights:release').id, identity)
        self.assertEqual([a.id for a in api.run('e/consumer/reader').used_artifacts()], [identity])
        self.assertEqual(len(list(api.run('offline-owner').logged_artifacts())), 2)

    def test_lost_ack_retries_from_transfer_after_producer_files_are_removed(self):
        artifact, owner = self.producer()
        reader = self.consumer()
        publish = self.sink.publish
        def lost(events):
            publish(events)
            if any(e['type']=='artifact' for e in events):
                raise OSError('publication acknowledgement lost')
        with patch.object(self.sink, 'publish', side_effect=lost):
            with self.assertRaises(w.Error):
                reader.use_artifact(artifact)
        self.assertEqual(artifact.state, 'PENDING')
        self.assertFalse(any(e['type']=='artifact_use' for e in read_journal(reader.sync_dir)))
        shutil.rmtree(owner.sync_dir)
        self.assertIs(reader.use_artifact(artifact), artifact)
        reader.finish()
        manifests = [e for e in self.sink.events if e['type']=='artifact']
        self.assertEqual(len(manifests), 2)
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(artifact.get_entry('nested/weights.bin')._read_verified_bytes(), b'frozen offline model')

    def test_corrupt_source_cannot_publish_any_artifact_or_input(self):
        artifact, owner = self.producer()
        manifest = next(e['payload'] for e in read_journal(owner.sync_dir) if e['type']=='artifact' and e['payload']['artifact_id']==artifact._id)
        Path(owner.sync_dir, manifest['files'][0]['_backprop_local_file']).write_bytes(b'corrupt')
        reader = self.consumer()
        with self.assertRaisesRegex(w.UsageError, 'SHA-256/size'):
            reader.use_artifact(artifact)
        self.assertFalse(any(e['type'] in {'artifact', 'artifact_use'} for e in self.sink.events))
        self.assertEqual(artifact.state, 'PENDING')

    def test_restored_local_manifest_and_empty_manifest_are_usable(self):
        for empty in [False, True]:
            with self.subTest(empty=empty):
                artifact, owner = self.producer(empty=empty)
                events = read_journal(owner.sync_dir)
                event = next(e for e in events if e['type']=='artifact' and e['payload']['artifact_id']==artifact._id)
                restored = w.Artifact._from_manifest(event['payload'], JournalArtifactStore(owner.sync_dir, events, None), 'e', 'source', owner.id)
                restored._committed = empty
                reader = self.consumer('restored-' + str(empty))
                self.assertIs(reader.use_artifact(restored), restored)
                self.assertEqual(restored.state, 'COMMITTED')
                self.assertEqual(w.Api._from_store(self.sink,entity='e',project='source').artifact(restored._id).id,restored._id)
                reader.finish()

    def test_transfer_journal_restarts_in_independent_process_without_source(self):
        artifact, owner = self.producer()
        reader = self.consumer()
        with patch.object(self.sink, 'upload_file', side_effect=OSError('interrupted upload')):
            with self.assertRaises(w.Error):
                reader.use_artifact(artifact)
        transfer = artifact._handoff_journal
        shutil.rmtree(owner.sync_dir)
        script = """
import json,sys
import backprop as w
sink=w.MemorySink();w.sync(sys.argv[1],sink=sink);w.sync(sys.argv[1],sink=sink)
event=next(e for e in sink.events if e['type']=='artifact')
a=w.Api._from_store(sink,entity='e',project='source').artifact(event['payload']['artifact_id'])
assert a.get_entry('nested/weights.bin')._read_verified_bytes()==b'frozen offline model'
print(json.dumps({'events':sink.events,'files':{name:data.hex() for name,data in sink.files.items()}}))
"""
        result = subprocess.run([sys.executable,'-B','-c',script,transfer],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,
                                env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
        self.assertEqual(result.returncode,0,result.stderr)
        record=json.loads(result.stdout)
        self.assertEqual(len(record['events']),1)
        self.sink.events.extend(record['events']);self.sink.files.update({name:bytes.fromhex(value) for name,value in record['files'].items()})
        self.assertIs(reader.use_artifact(artifact),artifact)
        reader.finish()
        self.assertEqual([a.id for a in w.Api._from_store(self.sink,entity='e',project='consumer').run('reader').used_artifacts()],[artifact.id])

    def test_model_helper_downloads_the_offline_snapshot_through_each_entry(self):
        artifact, owner = self.producer()
        reader = self.consumer(global_run=True)
        with patch('backprop.artifacts.Artifact._default_root', return_value=str(self.directory / 'download')):
            self.assertEqual(Path(reader.use_model(artifact),'weights.bin').read_bytes(), b'frozen offline model')
            self.assertEqual(Path(w.use_model(artifact),'weights.bin').read_bytes(), b'frozen offline model')
        reader.finish()
        self.assertFalse(any(e['type']=='history' and e['run_id']==owner.id for e in self.sink.events))

    def test_hard_process_exit_and_shared_browser_replay_fixture(self):
        from artifact_handoff_replay_fixture import build
        result = build()
        self.assertTrue(result['restarted_after_hard_exit'])
        self.assertTrue(result['source_was_unsynced_until_explicit_sync'])
        self.assertTrue(result['file_bytes_verified'])
        self.assertEqual(result['used_artifact_count'],1)

    def test_two_processes_share_one_transfer_journal_without_duplicate_append(self):
        from artifact_handoff_replay_fixture import DiskSink
        artifact, owner = self.producer()
        consumer_dir = self.directory / 'concurrent-consumer'
        remote_dir = self.directory / 'remote'
        script = """
import sys
from types import SimpleNamespace
import backprop as w
from backprop.config_artifacts import JournalArtifactStore
from backprop.artifact_handoff import publish_pending
from backprop.transport import Journal,read_journal
from artifact_handoff_replay_fixture import DiskSink
events=read_journal(sys.argv[1]);event=next(e for e in events if e['type']=='artifact' and e['payload']['name']=='weights')
a=w.Artifact._from_manifest(event['payload'],JournalArtifactStore(sys.argv[1],events,None),'e','source','offline-owner');a._committed=False
sink=DiskSink(sys.argv[3]);run=SimpleNamespace(_sink=sink,_journal=Journal(sys.argv[2],sink))
publish_pending(a,run);assert a.state=='COMMITTED'
"""
        env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
        children=[subprocess.Popen([sys.executable,'-B','-c',script,owner.sync_dir,str(consumer_dir),str(remote_dir)],cwd=tempfile.gettempdir(),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        for child in children:
            stdout,stderr=child.communicate(timeout=30)
            self.assertEqual(child.returncode,0,stderr)
        journals=list(consumer_dir.glob('artifact-inputs/*/events.jsonl'))
        self.assertEqual(len(journals),1)
        self.assertEqual(len(read_journal(journals[0].parent)),1)
        sink=DiskSink(remote_dir)
        self.assertEqual(len(sink.read_events()),1)
        self.assertEqual(w.Api._from_store(sink,entity='e',project='source').artifact(artifact._id).get_entry('nested/weights.bin')._read_verified_bytes(),b'frozen offline model')

    def test_pending_descriptive_changes_persist_without_changing_content_version(self):
        artifact, owner = self.producer()
        original_version = artifact.version
        artifact.metadata['revision'] = 2
        artifact.description = 'edited before online use'
        artifact.tags = ['selected']
        reader = self.consumer()
        reader.use_artifact(artifact)
        found = w.Api._from_store(self.sink,entity='e',project='source').artifact(artifact.id)
        self.assertEqual(found.metadata,{'revision':2})
        self.assertEqual(found.description,'edited before online use')
        self.assertEqual(found.tags,['selected'])
        self.assertEqual(found.version,original_version)
        self.assertEqual(found.get_entry('nested/weights.bin')._read_verified_bytes(),b'frozen offline model')
