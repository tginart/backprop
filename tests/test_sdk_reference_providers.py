"""Pinned cloud reference decisions against controlled provider clients.

Architecture: the unchanged handler bodies call client fixtures that expose only
provider APIs, so metadata/listing/version behavior is testable without cloud
access. A separate installed-runtime suite validates real provider object and
method contracts. No fixture consults ambient credentials or endpoint metadata.
"""
import io
import sys
import types
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import backprop
from backprop.artifacts import ArtifactManifestEntry
from backprop.reference_handlers.s3_handler import S3Handler
from backprop.reference_handlers.gcs_handler import GCSHandler
from backprop.reference_handlers.azure_handler import AzureHandler
from backprop.external_references import add_external_reference, read_external_reference


class ClientError(Exception):
    def __init__(self,code='404'):
        self.response={'Error':{'Code':code,'Message':'fixture provider failure'}}


class S3Objects(list):
    def filter(self,**kwargs): return S3Objects(item for item in self if item.key.startswith(kwargs['Prefix']))
    def limit(self,count): return S3Objects(self[:count])


class S3Object:
    def __init__(self,key,data=b'bytes',version='7',etag='v1',exists=True):
        self.key,self.data,self.version_id=key,data,version
        self.e_tag='"'+etag+'"';self.size=self.content_length=len(data)
        self.content_type='application/octet-stream';self.exists=exists
    def load(self):
        if not self.exists: raise ClientError()
    def download_fileobj(self,stream,ExtraArgs):stream.write(self.data)
    def Object(self):return self
    def head(self):return {'ETag':self.e_tag}


class CloudReferenceContracts(unittest.TestCase):
    def test_s3_false_canonical_uri_and_true_version_metadata(self):
        handler=S3Handler();obj=S3Object('prefix/data')
        handler._s3=SimpleNamespace(Object=lambda *args:obj)
        handler._botocore=SimpleNamespace(exceptions=SimpleNamespace(ClientError=ClientError))
        false=handler.store_path(None,'s3://bucket/prefix/data?versionId=7',checksum=False)[0]
        self.assertEqual((false.path,false.ref,false.digest,false.size),('prefix/data','s3://bucket/prefix/data','s3://bucket/prefix/data',None))
        true=handler.store_path(None,'s3://bucket/prefix/data')[0]
        self.assertEqual((true.path,true.digest,true.size,true.extra),('data','v1',5,{'etag':'v1','versionID':'7'}))
        self.assertEqual(handler.store_path(None,'s3://bucket',checksum=False)[0].path,'bucket')

    def test_s3_prefix_limit_and_old_version_recovery(self):
        handler=S3Handler();current=S3Object('prefix/file',etag='new',data=b'new')
        old=S3Object('prefix/file',etag='old',data=b'old')
        objects=S3Objects([S3Object('prefix/a'),S3Object('prefix/empty',data=b''),S3Object('prefix/b')])
        bucket=SimpleNamespace(objects=objects,object_versions=S3Objects([old]))
        handler._s3=SimpleNamespace(Object=lambda b,k:current if k=='prefix/file' else S3Object(k,exists=False),Bucket=lambda b:bucket)
        handler._botocore=SimpleNamespace(exceptions=SimpleNamespace(ClientError=ClientError))
        entries=handler.store_path(None,'s3://bucket/prefix',name='out',max_objects=2)
        # Source applies the limit before dropping zero-length objects.
        self.assertEqual([entry.path for entry in entries],['out/a'])
        entry=ArtifactManifestEntry(path='file',ref='s3://bucket/prefix/file',digest='old',extra={'etag':'old'})
        try:self.assertEqual(Path(handler.load_path(entry,local=True)).read_bytes(),b'old')
        finally:handler._cache.close()
        entry=ArtifactManifestEntry(path='file',ref='s3://bucket/prefix/file',digest='missing',extra={'etag':'missing'})
        with self.assertRaises(FileNotFoundError):handler.load_path(entry,local=True)
        handler._cache.close()

    def test_gcs_false_generation_and_directory_metadata(self):
        handler=GCSHandler(); calls=[]
        obj=SimpleNamespace(name='prefix/file',etag='opaque-gcs',size=4,generation=123)
        directory=SimpleNamespace(name='prefix/',etag='directory',size=0,generation=124)
        def get_blob(key,generation=None):
            calls.append((key,generation));return obj if key=='prefix/file' and generation in (None,'123',123) else None
        bucket=SimpleNamespace(get_blob=get_blob,list_blobs=lambda **kwargs:[directory,obj])
        handler._client=SimpleNamespace(bucket=lambda name:bucket)
        false=handler.store_path(None,'gs://bucket/prefix/file#123',checksum=False)[0]
        self.assertEqual((false.path,false.ref,false.digest,false.size),('prefix/file','gs://bucket/prefix/file','gs://bucket/prefix/file',None))
        self.assertEqual(calls,[])
        entry=handler.store_path(None,'gs://bucket/prefix/file#123')[0]
        self.assertEqual(entry.extra,{'versionID':123});self.assertEqual(calls[-1],('prefix/file','123'))
        entries=handler.store_path(None,'gs://bucket/prefix',name='nested')
        self.assertEqual([e.path for e in entries],['nested/file'])
        with self.assertRaisesRegex(ValueError,'Object does not exist'):
            handler.store_path(None,'gs://bucket/absent#9')

    def test_gcs_download_falls_back_from_missing_generation_then_checks_etag(self):
        handler=GCSHandler();obj=SimpleNamespace(etag='gcs',download_to_file=lambda f:f.write(b'content'))
        calls=[]
        def get_blob(key,generation=None):calls.append(generation);return obj if generation is None else None
        handler._client=SimpleNamespace(bucket=lambda name:SimpleNamespace(get_blob=get_blob))
        entry=ArtifactManifestEntry(path='file',ref='gs://bucket/file.bin',size=7,digest='gcs',extra={'versionID':9})
        try:self.assertEqual(Path(handler.load_path(entry,local=True)).read_bytes(),b'content')
        finally:handler._cache.close()
        self.assertEqual(calls,[9,None])
        obj.etag='changed'
        with self.assertRaisesRegex(ValueError,'Digest mismatch'):handler.load_path(entry,local=True)
        handler._cache.close()

    def test_azure_uri_normalization_false_does_not_construct_client(self):
        handler=AzureHandler()
        with patch.object(handler,'_get_module',side_effect=AssertionError('no client')):
            entry=handler.store_path(None,'https://account.blob.core.windows.net/container/path/file?versionId=v1&sig=unused',checksum=False)[0]
        self.assertEqual((entry.path,entry.digest,entry.size),('path/file','https://account.blob.core.windows.net/container/path/file',None))
        self.assertNotIn('sig',entry.ref)

    def test_azure_properties_version_prefix_limit_warning_and_credentials(self):
        handler=AzureHandler();calls=[]
        def properties(name,size=7,folder=False):
            return SimpleNamespace(name=name,size=size,etag='"azure-v1"',version_id='123',metadata={'hdi_isfolder':'true'} if folder else {},has_key=lambda key:key=='metadata')
        blob=SimpleNamespace(exists=lambda **kw:True,get_blob_properties=lambda **kw:properties('prefix/file'))
        container=SimpleNamespace(list_blobs=lambda **kw:[properties('prefix/a'),properties('prefix/b')])
        service=SimpleNamespace(get_blob_client=lambda **kw:blob,get_container_client=lambda name:container)
        module=types.ModuleType('azure.storage.blob')
        module.BlobServiceClient=lambda url,credential:(calls.append((url,credential)) or service)
        with patch.object(handler,'_get_module',return_value=module),patch.object(handler,'_get_credential',return_value='fixture-key'):
            entry=handler.store_path(None,'https://account.blob.core.windows.net/container/prefix/file?versionId=123')[0]
            self.assertEqual((entry.path,entry.digest,entry.extra),('file','azure-v1',{'etag':'azure-v1','versionID':'123'}))
            blob.exists=lambda **kw:False
            with patch('backprop.termwarn') as warning:
                entries=handler.store_path(None,'https://account.blob.core.windows.net/container/prefix',name='out',max_objects=1)
                self.assertEqual([e.path for e in entries],['out/a']);warning.assert_called_once()
        run=SimpleNamespace(settings=SimpleNamespace(azure_account_url_to_access_key={'https://account.blob.core.windows.net':'private-key'}))
        with patch.object(backprop,'run',run),patch.object(handler,'_get_module',side_effect=AssertionError('no default credentials')):
            self.assertEqual(handler._get_credential('https://account.blob.core.windows.net'),'private-key')
        self.assertEqual(calls[0],('https://account.blob.core.windows.net','fixture-key'))

    def test_provider_dispatch_priority_and_failure_atomicity(self):
        artifact=backprop.Artifact('provider','dataset');handler=GCSHandler()
        with patch('backprop.external_references._handler',return_value=handler),patch.object(handler,'store_path',side_effect=RuntimeError('provider')):
            with self.assertRaisesRegex(RuntimeError,'provider'):
                artifact.add_reference('gs://bucket/path')
        self.assertEqual(dict(artifact.manifest.entries),{})
        with patch('backprop.reference_handlers.azure_handler.AzureHandler.store_path',return_value=[]) as azure,patch('backprop.reference_handlers.http_handler.HTTPHandler.store_path',side_effect=AssertionError('generic http')):
            artifact.add_reference('https://account.blob.core.windows.net/container/blob')
            azure.assert_called_once()


if __name__=='__main__':unittest.main()
