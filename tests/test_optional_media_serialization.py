"""Base value parity and durable media provenance without another service.

Architecture: isolated genuine W&B comparisons -> native artifact operations ->
retry, delayed visibility and process-crash replay. Main callers: local SDK and
optional-framework CI. The installed environments are used without modification.
"""
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.media_base import Media
from backprop.errors import CommError

ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}


class MediaSerializationContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'BACKPROP_DIR':self.temp.name});env.start();self.addCleanup(env.stop)
        self.store=w.MemorySink();self.api=w.Api._from_store(self.store,entity='e',project='base-media')

    def writer(self,identity='media',settings=None,**kwargs):
        run=w.Run(id=identity,entity='e',project='base-media',dir=self.temp.name,
            settings={**SETTINGS,**(settings or {})},_deployment={},_sink=self.store,**kwargs)
        self.addCleanup(lambda:run.finish() if not run._closed else None)
        return run

    def test_genuine_public_contracts_and_independent_imports(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/media_serialization_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                output=ROOT/'test-results';output.mkdir(exist_ok=True)
                (output/f'media-serialization-{mode}.json').write_text(result.stdout)
        self.assertEqual(observations[0],observations[1])
        self.assertEqual(len(observations[0]['values']),2)
        self.assertEqual(len(observations[0]['suffixes']),29)
        self.assertEqual(len(observations[0]['media']),4)
        self.assertEqual(len(observations[0]['arrays']),27)

    def test_real_reference_reuse_survives_delayed_duplicate_events_and_resume(self):
        run=self.writer();source=w.Artifact('source-media','dataset')
        source.add(w.Html('<p>source</p>',inject=False),'html');run.log_artifact(source);run.flush()
        earlier=copy.deepcopy(self.store.events)
        saved=self.api.artifact(source.id);value=saved.get('html')
        content=value._artifact_content_path;entry=saved.get_entry(content)
        target=w.Artifact('copied-media','dataset')
        # Reuse the capability already held by the decoded value; a separate
        # global store lookup would break offline and mixed-credential callers.
        with patch('backprop.artifact_references.native_reference_entry',side_effect=AssertionError('Unexpected URL query')):
            result=Media.to_json(value,target)
        copied=target.get_entry(result['path'])
        self.assertEqual(copied._read_verified_bytes(),b'<p>source</p>')
        self.assertEqual(copied.ref,entry.ref_url());self.assertEqual(copied.birth_artifact_id,source.id)
        original_count=len(target.manifest.entries)
        self.assertEqual(Media.to_json(value,target),result)
        self.assertEqual(len(target.manifest.entries),original_count)
        run.log_artifact(target)
        publish=self.store.publish
        def lose_ack(events):publish(events);raise OSError('acknowledgement lost')
        with patch.object(self.store,'publish',side_effect=lose_ack):
            with self.assertRaises(w.Error):run.flush()
        run.flush();run.finish()
        final=copy.deepcopy(self.store.events);self.store.events[:]=earlier
        with self.assertRaisesRegex(CommError,'Artifact not found'):self.api.artifact(target.id)
        self.store.events[:]=list(reversed(final))+final
        restored=w.Api._from_store(self.store,entity='e',project='base-media').artifact(target.id)
        self.assertEqual(restored.get_entry(content)._read_verified_bytes(),b'<p>source</p>')
        self.assertEqual(restored.get_entry(content).ref,entry.ref_url())
        resumed=self.writer(resume='allow');resumed.log({'resumed':True});resumed.finish()
        self.assertEqual(self.api.artifact(target.id).get_entry(content).sha256,copied.sha256)
        self.assertEqual(saved.get_entry(content).ref,entry.ref)

    def test_reference_failure_leaves_destination_empty_and_retry_preserves_identity(self):
        run=self.writer();source=w.Artifact('failed-source','dataset')
        source.add(w.Html('retry me',inject=False),'html');run.log_artifact(source);run.flush()
        value=self.api.artifact(source.id).get('html');target=w.Artifact('retry-target','dataset')
        original=value._artifact_source.get_entry(value._artifact_content_path)
        with patch.object(type(original),'_read_verified_bytes',side_effect=OSError('file temporarily unavailable')):
            with self.assertRaisesRegex(OSError,'temporarily unavailable'):Media.to_json(value,target)
        self.assertEqual(len(target.manifest.entries),0)
        result=Media.to_json(value,target)
        self.assertEqual(target.get_entry(result['path']).ref,original.ref_url())
        self.assertEqual(target.get_entry(result['path'])._read_verified_bytes(),b'retry me')

    def test_run_serialization_keeps_source_and_latest_fields_and_bound_path(self):
        run=self.writer();value=w.Html('value',inject=False);artifact=w.Artifact('target','dataset')
        artifact.add(value,'html');run.log_artifact(artifact)
        unbound=Media.to_json(value,run)
        self.assertTrue(unbound['artifact_path'].startswith('backprop-artifact://'))
        self.assertTrue(unbound['_latest_artifact_path'].endswith('?alias=latest'))
        self.assertNotIn('path',unbound)
        value.bind_to_run(run,'bound',0)
        bound=Media.to_json(value,run)
        self.assertEqual(bound['artifact_path'],unbound['artifact_path'])
        self.assertEqual(Path(run.dir,bound['path']).read_bytes(),b'value')
        run.finish()

    def test_shared_writers_reuse_same_source_without_changing_source_manifest(self):
        primary=self.writer('shared',mode='shared',settings={'x_primary':True,'x_label':'rank0'})
        source=w.Artifact('shared-source','dataset');source.add(w.Html('shared',inject=False),'html')
        primary.log_artifact(source);primary.flush()
        original=self.api.artifact(source.id);digest=original.digest
        for index in range(2):
            writer=self.writer('shared',mode='shared',settings={'x_primary':False,'x_label':'rank'+str(index+1)})
            value=w.Api._from_store(self.store,entity='e',project='base-media').artifact(source.id).get('html')
            target=w.Artifact('contribution-'+str(index),'dataset');descriptor=Media.to_json(value,target)
            writer.log_artifact(target);writer.finish()
            saved=self.api.artifact(target.id)
            self.assertEqual(saved.get_entry(descriptor['path']).birth_artifact_id,source.id)
        primary.finish();self.assertEqual(self.api.artifact(source.id).digest,digest)

    def test_process_crash_keeps_reused_reference_in_durable_journal(self):
        script='''
import json,os,sys
import backprop as w
from backprop.media_base import Media
run=w.Run(id='crash',entity='e',project='base-media',dir=sys.argv[1],settings=json.loads(sys.argv[2]),_deployment={},_sink=w.MemorySink())
source=w.Artifact('crash-source','dataset');source.add(w.Html('durable',inject=False),'html');run.log_artifact(source);run.flush()
value=source.get('html');target=w.Artifact('crash-copy','dataset');descriptor=Media.to_json(value,target);run.log_artifact(target)
print(json.dumps({'journal':str(run._journal.directory),'source':source.id,'target':target._id,'path':descriptor['path']}),flush=True)
os._exit(0)
'''
        result=subprocess.run([sys.executable,'-B','-c',script,self.temp.name,json.dumps(SETTINGS)],
            cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,
            env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
        self.assertEqual(result.returncode,0,result.stderr);saved=json.loads(result.stdout)
        count=w.sync(saved['journal'],sink=self.store);self.assertGreater(count,0)
        self.assertEqual(w.sync(saved['journal'],sink=self.store),count)
        entry=self.api.artifact(saved['target']).get_entry(saved['path'])
        self.assertEqual(entry._read_verified_bytes(),b'durable')
        self.assertEqual(entry.birth_artifact_id,saved['source'])
        self.assertIn(saved['source'],entry.ref)


if __name__=='__main__':unittest.main()
