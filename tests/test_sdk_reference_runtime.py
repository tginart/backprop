"""Installed provider-client contracts with no cloud requests or real credentials.

Architecture: genuine requests uses a loopback HTTP server; genuine boto3 uses
botocore Stubber; genuine Google/Azure clients parse controlled metadata while
read operations use autospecced provider methods. Explicit dummy credentials
prevent default chains/IMDS. These prove SDK signature/model compatibility, not
an authorized cloud-object integration. Main caller: isolated release runtime.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import backprop
from backprop.reference_handlers.s3_handler import S3Handler
from backprop.reference_handlers.gcs_handler import GCSHandler
from backprop.reference_handlers.azure_handler import AzureHandler


def available(module):
    try:importlib.import_module(module);return True
    except (ImportError,ModuleNotFoundError):return False


class ReferenceRuntime(unittest.TestCase):
    @unittest.skipUnless(available('requests'),'Existing requests runtime required')
    def test_genuine_http_metadata_only_roundtrip_and_etag_failure(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            etag='"v1"'
            def log_message(self,*args):pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200);self.send_header('Content-Length','7')
                if self.etag:self.send_header('ETag',self.etag)
                self.end_headers();self.wfile.write(b'content')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        uri=f'http://127.0.0.1:{server.server_port}/object'
        checked=backprop.Artifact('http','dataset');entry=checked.add_reference(uri)[0]
        self.assertEqual((entry.digest,entry.size,entry.extra),('v1',7,{'etag':'"v1"'}))
        self.assertEqual(len(requests),1)
        with tempfile.TemporaryDirectory() as directory:
            output=Path(entry.download(directory));self.assertEqual(output.read_bytes(),b'content')
            Handler.etag='"v2"'
            with self.assertRaisesRegex(ValueError,'Digest mismatch'):entry.download(directory)
            self.assertEqual(output.read_bytes(),b'content')
        unchecked=backprop.Artifact('unchecked','dataset')
        before=len(requests);false=unchecked.add_reference(uri,checksum=False)[0]
        self.assertEqual(len(requests),before)
        with self.assertRaisesRegex(ValueError,'Digest mismatch'):false._read_verified_bytes()
        Handler.etag=None
        self.assertEqual(false._read_verified_bytes(),b'content')

    @unittest.skipUnless(available('boto3'),'Existing boto3 runtime required')
    def test_genuine_boto3_metadata_version_and_download_via_stubber(self):
        import boto3,botocore
        from botocore.stub import Stubber
        from botocore.response import StreamingBody
        resource=boto3.resource('s3',region_name='us-east-1',aws_access_key_id='dummy',aws_secret_access_key='dummy')
        handler=S3Handler();handler._s3=resource;handler._botocore=botocore
        client=resource.meta.client
        with Stubber(client) as stub:
            metadata={'ContentLength':7,'ETag':'"v1"','ContentType':'application/octet-stream','VersionId':'7'}
            stub.add_response('head_object',metadata,{'Bucket':'bucket','Key':'path/file'})
            entry=handler.store_path(None,'s3://bucket/path/file')[0]
            self.assertEqual((entry.path,entry.digest,entry.size,entry.extra),('file','v1',7,{'etag':'v1','versionID':'7'}))
            stub.add_response('head_object',metadata,{'Bucket':'bucket','Key':'path/file','VersionId':'7'})
            stub.add_response('head_object',metadata,None)
            stub.add_response('get_object',{'Body':StreamingBody(io.BytesIO(b'content'),7),'ContentLength':7,'ETag':'"v1"'},None)
            try:self.assertEqual(Path(handler.load_path(entry,local=True)).read_bytes(),b'content')
            finally:handler._cache.close()
            before=handler.store_path(None,'s3://bucket/path/file?versionId=9',checksum=False)[0]
            self.assertEqual(before.digest,'s3://bucket/path/file')
            stub.assert_no_pending_responses()

    @unittest.skipUnless(available('google.cloud.storage'),'Existing google-cloud-storage runtime required')
    def test_genuine_google_metadata_models_and_download_signature(self):
        from google.cloud import storage
        from google.auth.credentials import AnonymousCredentials
        client=storage.Client(project='fixture',credentials=AnonymousCredentials())
        handler=GCSHandler();handler._client=client
        metadata={'name':'path/file','bucket':'bucket','etag':'gcs-etag','size':'7','generation':'123','kind':'storage#object'}
        with patch.object(client._connection,'api_request',return_value=metadata) as request:
            entry=handler.store_path(None,'gs://bucket/path/file#123')[0]
            self.assertEqual((entry.path,entry.digest,entry.size,entry.extra),('file','gcs-etag',7,{'versionID':123}))
            self.assertEqual(request.call_args.kwargs['query_params']['generation'],123)
            def download(blob,stream,*args,**kwargs):stream.write(b'content')
            with patch.object(storage.Blob,'download_to_file',autospec=True,side_effect=download) as read:
                try:self.assertEqual(Path(handler.load_path(entry,local=True)).read_bytes(),b'content')
                finally:handler._cache.close()
                self.assertEqual(read.call_args.args[0].generation,123)
            before=request.call_count
            self.assertEqual(handler.store_path(None,'gs://bucket/path/file#999',checksum=False)[0].digest,'gs://bucket/path/file')
            self.assertEqual(request.call_count,before)

    @unittest.skipUnless(available('azure.storage.blob') and available('azure.identity'),'Existing Azure blob/identity runtime required')
    def test_genuine_azure_client_models_and_versioned_read_signature(self):
        from azure.storage.blob import BlobServiceClient,BlobClient,BlobProperties,StorageStreamDownloader
        handler=AzureHandler();uri='https://fixture.blob.core.windows.net/container/path/file?versionId=7'
        properties=BlobProperties(**{'name':'path/file','Content-Length':7,'ETag':'"azure-etag"','x-ms-version-id':'7','metadata':{}})
        key=base64.b64encode(b'fixture dummy account key').decode()
        with patch.object(handler,'_get_credential',return_value=key),patch.object(BlobClient,'exists',autospec=True,return_value=True) as exists,patch.object(BlobClient,'get_blob_properties',autospec=True,return_value=properties):
            entry=handler.store_path(None,uri)[0]
            self.assertEqual((entry.path,entry.digest,entry.size,entry.extra),('file','azure-etag',7,{'etag':'azure-etag','versionID':'7'}))
            self.assertEqual(exists.call_args.kwargs,{'version_id':'7'})
            # Autospec enforces the real downloaded-stream readinto signature.
            from unittest.mock import create_autospec
            downloader=create_autospec(StorageStreamDownloader,instance=True)
            downloader.readinto.side_effect=lambda stream:stream.write(b'content')
            with patch.object(BlobClient,'download_blob',autospec=True,return_value=downloader) as download:
                try:self.assertEqual(Path(handler.load_path(entry,local=True)).read_bytes(),b'content')
                finally:handler._cache.close()
                self.assertEqual(download.call_args.kwargs,{'version_id':'7'})


if __name__=='__main__':unittest.main()
