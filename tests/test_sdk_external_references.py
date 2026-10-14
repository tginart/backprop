"""Source-pinned external manifests and provider reads without cloud credentials.

Architecture: assert retained handler bodies against pinned hashes, exercise
metadata and integrity decisions with provider fixtures, then log/read through
real Backprop journals. Local HTTP tests use a loopback server only when requests
already exists. No install, cloud access, new storage, or credential discovery.
"""
import ast
import base64
import copy
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk'))
import backprop
from backprop.external_references import MARKER, is_external

SOURCE = Path(__file__).resolve().parents[1] / '.reference/wandb/source/wandb/sdk/artifacts/storage_handlers'
NATIVE = Path(__file__).resolve().parents[1] / 'sdk/backprop/reference_handlers'
HANDLERS = ['local_file_handler', 'http_handler', 'tracking_handler', 's3_handler', 'gcs_handler', 'azure_handler']


class Response:
    def __init__(self, uri, data=b'content', etag='"v1"'):
        self.request = SimpleNamespace(url=uri)
        self.headers = {'content-length': str(len(data))}
        if etag is not None:
            self.headers['etag'] = etag
        self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, chunk_size): yield self.data


class ExternalReferenceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.bin'; self.source.write_bytes(b'content')
        self.store = backprop.MemorySink()
        self.run = backprop.Run(project='references', entity='owner', id='writer', dir=self.root,
            _sink=self.store, _deployment={}, settings={'console':'off','silent':True,'disable_git':True,'label_disable':True})
        self.addCleanup(lambda: None if self.run._closed else self.run.finish())
        self.api = backprop.Api._from_store(self.store, entity='owner', project='references')

    def publish(self, artifact):
        with patch.object(self.run._journal, 'stage_file', side_effect=AssertionError('reference bytes uploaded')):
            self.run.log_artifact(artifact)
        self.run.flush()
        return self.api.artifact(artifact.id)

    @unittest.skipUnless(SOURCE.is_dir(), 'Pinned W&B0.30 source required')
    def test_handler_bodies_are_exact_pinned_source(self):
        for name in HANDLERS:
            with self.subTest(handler=name):
                source = (SOURCE / (name+'.py')).read_bytes()
                native = (NATIVE / (name+'.py')).read_text()
                self.assertIn(hashlib.sha256(source).hexdigest(), native)
                def bodies(text):
                    return [ast.dump(node, include_attributes=False) for node in ast.parse(text).body
                            if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
                self.assertEqual(bodies(source), bodies(native))

    def test_http_false_has_no_request_unknown_size_and_roundtrips_without_blob(self):
        uri = 'https://example.invalid/object?version=4'
        artifact = backprop.Artifact('external','dataset')
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', side_effect=AssertionError('metadata fetch')):
            entry = artifact.add_reference(uri, checksum=False)[0]
            self.assertEqual((entry.path, entry.digest, entry.ref, entry.size), ('object?version=4',uri,uri,None))
            loaded = self.publish(artifact)
            draft = loaded.new_draft()
        current = loaded.get_entry(entry.path)
        self.assertTrue(is_external(current)); self.assertEqual(loaded.size,0)
        self.assertEqual(draft.get_entry(entry.path).digest,uri)
        descriptor = next(event for event in self.store.events if event['type']=='artifact')['payload']['files'][0]
        self.assertTrue(descriptor[MARKER]); self.assertNotIn('dataset_rid',descriptor)
        self.assertNotIn('sha256',descriptor); self.assertEqual(descriptor['uri'],uri)
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', return_value=Response(uri,etag=None)) as fetch:
            self.assertEqual(current._read_verified_bytes(),b'content')
            public_file = next(iter(loaded.files()))
            self.assertTrue(public_file.id); self.assertEqual(public_file.url,uri)
            expected=base64.b64encode(hashlib.md5(b'content',usedforsecurity=False).digest()).decode()
            reads=fetch.call_count
            self.assertEqual(public_file.md5,expected)
            self.assertNotEqual(public_file.md5,uri)
            self.assertEqual(fetch.call_count,reads+1)
            with public_file.download(self.root/'public') as stream:
                self.assertEqual(stream.read(),'content')
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', return_value=Response(uri,etag='"new"')):
            with self.assertRaisesRegex(ValueError,'Digest mismatch'):
                current.download(self.root/'public')
        self.assertEqual((self.root/'public'/entry.path).read_bytes(),b'content')

    def test_http_true_tracks_headers_and_checks_current_etag(self):
        uri='https://example.invalid/object'
        artifact=backprop.Artifact('headers','dataset')
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', return_value=Response(uri)):
            entry=artifact.add_reference(uri)[0]
        self.assertEqual((entry.digest,entry.size,entry.extra),('v1',7,{'etag':'"v1"'}))
        loaded=self.publish(artifact)
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', return_value=Response(uri)) as fetch:
            self.assertEqual(loaded.get_entry('object')._read_verified_bytes(),b'content')
            public_file=next(iter(loaded.files()))
            reads=fetch.call_count
            digest=public_file.md5
            self.assertNotEqual(digest,'v1');self.assertEqual(public_file.md5,digest)
            self.assertEqual(fetch.call_count,reads+1)
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', return_value=Response(uri,etag='"v2"')):
            with self.assertRaisesRegex(ValueError,'Digest mismatch'):
                loaded.get_entry('object')._read_verified_bytes()

    def test_file_false_uses_path_hash_and_preserves_source_download_quirk(self):
        artifact=backprop.Artifact('paths','dataset')
        entry=artifact.add_reference(self.source.as_uri(),checksum=False)[0]
        expected=base64.b64encode(hashlib.md5(self.source.resolve().as_uri().encode(),usedforsecurity=False).digest()).decode()
        self.assertEqual((entry.digest,entry.size),(expected,7))
        loaded=self.publish(artifact)
        with self.assertRaisesRegex(ValueError,'Digest mismatch'):
            loaded.get_entry(entry.path).download(self.root/'download')
        original=artifact.digest
        manifest_digest=artifact.manifest.digest()
        self.source.write_bytes(b'CONTENT')
        again=backprop.Artifact('paths','dataset');again.add_reference(self.source.as_uri(),checksum=False)
        self.assertEqual(original,again.digest)
        self.source.write_bytes(b'content with a different size')
        resized=backprop.Artifact('paths','dataset');resized.add_reference(self.source.as_uri(),checksum=False)
        self.assertEqual(manifest_digest,resized.manifest.digest())
        self.assertNotEqual(artifact.get_entry(entry.path).size,resized.get_entry(entry.path).size)
        # Native version IDs include the immutable captured stat metadata;
        # the public W&B-style manifest checksum retains URI-only identity.
        self.assertNotEqual(original,resized.digest)

    def test_file_directory_false_expands_and_max_objects_failure_is_atomic(self):
        directory=self.root/'directory';directory.mkdir()
        (directory/'a').write_text('a');(directory/'b').write_text('b')
        artifact=backprop.Artifact('directory','dataset')
        with self.assertRaisesRegex(ValueError,'Exceeded 1'):
            artifact.add_reference(directory.as_uri(),checksum=False,max_objects=1)
        self.assertEqual(dict(artifact.manifest.entries),{})
        entries=artifact.add_reference(directory.as_uri(),name='prefix',checksum=False,max_objects=2)
        self.assertEqual({entry.path for entry in entries},{'prefix/a','prefix/b'})

    def test_native_ref_copy_of_external_manifest_preserves_kind_without_fetch(self):
        source=backprop.Artifact('source','dataset')
        source.add_reference(self.source.as_uri())
        loaded=self.publish(source)
        target=backprop.Artifact('copy','dataset');target._store=self.store
        original=loaded.get_entry('source.bin')
        self.source.unlink()
        copied=target.add_reference(original.ref_url(),name='renamed',checksum=False)[0]
        self.assertTrue(is_external(copied));self.assertEqual(copied.birth_artifact_id,source.id)
        self.assertEqual(copied.ref,original.ref_url())
        result=self.publish(target)
        with self.assertRaisesRegex(ValueError,'Failed to find'):
            result.get_entry('renamed')._read_verified_bytes()
        result.verify(self.root/'not-downloaded')

    def test_unknown_scheme_requires_name_warns_and_refuses_download(self):
        artifact=backprop.Artifact('tracking','dataset')
        with self.assertRaisesRegex(ValueError,'must pass name'):
            artifact.add_reference('nfs://host/path',checksum=False)
        with patch('backprop.termwarn') as warning:
            entry=artifact.add_reference('nfs://host/path',name='link')[0]
            warning.assert_called_once()
        self.assertEqual(entry.digest,'nfs://host/path')
        self.publish(artifact)
        with self.assertRaisesRegex(ValueError,'scheme.*not recognized'):
            entry._read_verified_bytes()

    def test_reference_identity_and_conflict_check_use_uri_without_fake_content_hash(self):
        first=backprop.Artifact('identity','dataset');second=backprop.Artifact('identity','dataset')
        with patch('backprop.reference_handlers.runtime.HTTPSession.get', side_effect=lambda uri,**kw:Response(uri)):
            first.add_reference('https://example.invalid/a',name='file')
            second.add_reference('https://example.invalid/b',name='file')
        self.assertNotEqual(first.digest,second.digest)
        before=first.manifest.to_manifest_json()
        with self.assertRaises(ValueError):
            first.add_reference('https://example.invalid/other',name='file',checksum=False)
        self.assertEqual(first.manifest.to_manifest_json(),before)

    def test_initialized_azure_credentials_select_provider_without_persisting_key(self):
        import types
        from backprop.notebook_snapshot import NotebookRedactor
        account='https://account.blob.core.windows.net'
        key='azure-private-key-for-redaction-test'
        captured=[]
        properties=SimpleNamespace(name='blob',size=7,etag='"version"',version_id='7',metadata={},has_key=lambda name:True)
        client=SimpleNamespace(exists=lambda **kwargs:True,get_blob_properties=lambda **kwargs:properties)
        service=SimpleNamespace(get_blob_client=lambda **kwargs:client)
        module=types.ModuleType('azure.storage.blob')
        module.BlobServiceClient=lambda url,credential:(captured.append((url,credential)) or service)
        with backprop.init(project='azure-setting',dir=self.root/'azure',_sink=self.store,_deployment={},
                settings={'azure_account_url_to_access_key':{account:key},'silent':True,'console':'off','label_disable':True,'disable_git':True}) as run:
            artifact=backprop.Artifact('azure','dataset')
            with patch('backprop.reference_handlers.azure_handler.AzureHandler._get_module',return_value=module):
                artifact.add_reference(account+'/container/blob')
            self.assertEqual(captured,[(account,key)])
            self.assertEqual(NotebookRedactor(run).text('source '+key),'source [redacted]')
            run._debug.internal('provider error '+key)
            run.log_artifact(artifact)
        self.assertNotIn(key,json.dumps(self.store.events))
        for path in (self.root/'azure').rglob('*'):
            if path.is_file():self.assertNotIn(key.encode(),path.read_bytes(),str(path))

    def test_artifact_download_explicit_missing_skip_and_gcs_directory_behavior(self):
        from backprop.reference_handlers.gcs_handler import _GCSIsADirectoryError
        from backprop.artifacts import ArtifactManifestEntry
        artifact=backprop.Artifact('missing','dataset')
        artifact.add_reference('https://example.invalid/a',checksum=False)
        with patch.object(ArtifactManifestEntry,'_read_verified_bytes',side_effect=FileNotFoundError('missing provider')):
            with self.assertRaises(FileNotFoundError):artifact.download(self.root/'absent')
            with patch('backprop.termwarn') as warning:
                artifact.download(self.root/'absent',allow_missing_references=True)
                warning.assert_called_once_with('missing provider')
        with patch.object(ArtifactManifestEntry,'_read_verified_bytes',side_effect=_GCSIsADirectoryError('folder')):
            artifact.download(self.root/'folder')
            checkout=self.root/'checkout';checkout.mkdir()
            (checkout/'a').write_text('existing tracked path')
            (checkout/'untracked').write_text('remove me')
            artifact.checkout(checkout)
            self.assertEqual((checkout/'a').read_text(),'existing tracked path')
            self.assertFalse((checkout/'untracked').exists())
        (checkout/'untracked').write_text('preserve on unexpected failure')
        with patch.object(artifact,'_download_entries',return_value=set()):
            with self.assertRaisesRegex(FileNotFoundError,'staged file is missing'):
                artifact.checkout(checkout)
        self.assertEqual((checkout/'untracked').read_text(),'preserve on unexpected failure')


if __name__=='__main__': unittest.main()
