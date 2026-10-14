"""Shared media-dataset contention retries only a known idempotent upload.

Architecture: actual HTTPError parsing -> fixed request retry budget -> stable
blob content and Journal event IDs. A failed implicit transaction is never
aborted by this caller, and unrelated 409 responses are never retried.
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
from backprop.foundry import FoundryStore, FoundryError
from backprop.transport import Journal, BackpropError

CONFIG={'host':'https://foundry.example.com','stream_dataset_rid':'ri.foundry.main.dataset.events','media_dataset_rid':'ri.foundry.main.dataset.media'}


class Response:
    headers={'Content-Type':'application/json'}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return b'{}'


def conflict(url='https://foundry.example.com',name='OpenTransactionAlreadyExists',status=409,body=None):
    data=json.dumps({'errorCode':'CONFLICT','errorName':name,'parameters':{'example':'private-token'}}).encode() if body is None else body
    return HTTPError(url,status,'Conflict',{},io.BytesIO(data))


class UploadRetry(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.file=self.root/'blob.bin';self.file.write_bytes(b'fixed bytes')
        self.store=FoundryStore(CONFIG,token='private-token')
        self.upload_path='/api/v2/datasets/'+CONFIG['media_dataset_rid']+'/files/'+quote('blobs/'+'a'*64+'/blob.bin',safe='')+'/upload?branchName=master'

    def test_known_conflict_retries_same_bytes_and_path_then_succeeds(self):
        calls=[]
        def open_request(request,**kwargs):
            calls.append(request)
            if len(calls)<3:raise conflict()
            return Response()
        with patch.object(self.store.opener,'open',side_effect=open_request),patch('backprop.foundry.time.sleep') as sleep:
            reference=self.store.upload_file(self.file)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,2])
        self.assertEqual(len({request.full_url for request in calls}),1)
        self.assertTrue(all(request.data==b'fixed bytes' and request.method=='POST' for request in calls))
        self.assertEqual(reference['size'],11)
        self.assertTrue(all('/upload?' in request.full_url for request in calls))

    def test_exhaustion_is_bounded_retryable_and_redacted(self):
        with patch.object(self.store.opener,'open',side_effect=lambda *a,**k:(_ for _ in ()).throw(conflict())) as opened,patch('backprop.foundry.time.sleep') as sleep:
            with self.assertRaises(FoundryError) as caught:self.store.upload_file(self.file)
        self.assertEqual(opened.call_count,4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,2,4])
        self.assertTrue(caught.exception.retryable);self.assertEqual(caught.exception.status_code,409)
        self.assertNotIn('private-token',str(caught.exception))

    def test_unknown_conflicts_malformed_bodies_and_wrong_routes_never_retry(self):
        cases=[(self.upload_path,'POST','DifferentConflict',409,None),
               (self.upload_path,'POST','OpenTransactionAlreadyExists',403,None),
               (self.upload_path,'POST','OpenTransactionAlreadyExists',409,b'not JSON OpenTransactionAlreadyExists'),
               (self.upload_path,'POST','OpenTransactionAlreadyExists',409,b'{"message":"OpenTransactionAlreadyExists"}'),
               (self.upload_path,'GET','OpenTransactionAlreadyExists',409,None),
               (self.upload_path.replace('branchName=master','branchName=other'),'POST','OpenTransactionAlreadyExists',409,None),
               (self.upload_path.replace('dataset.media','dataset.other'),'POST','OpenTransactionAlreadyExists',409,None),
               (self.upload_path.replace('blobs%2F','arbitrary%2F'),'POST','OpenTransactionAlreadyExists',409,None),
               (self.store.stream_path+'/publishRecords','POST','OpenTransactionAlreadyExists',409,None)]
        for path,method,name,status,body in cases:
            with self.subTest(path=path,method=method,name=name,status=status,body=body),patch.object(self.store.opener,'open',side_effect=conflict(name=name,status=status,body=body)) as opened,patch('backprop.foundry.time.sleep') as sleep:
                with self.assertRaises(FoundryError) as caught:self.store.request(path,method,b'bytes',retry=True)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(opened.call_count,1);sleep.assert_not_called()

    def test_exhausted_upload_retains_journal_then_retries_original_event_once(self):
        journal=Journal(self.root/'run',self.store)
        reference=journal.stage_file(self.file,name='blob.bin')
        original={'schema_version':1,'event_id':'stable-id','run_id':'run','entity':'owner','project':'test','type':'file','timestamp':'2026-10-03T01:00:00Z','payload':{'name':'blob.bin','file':reference}}
        journal.append(original)
        with patch.object(self.store.opener,'open',side_effect=lambda *a,**k:(_ for _ in ()).throw(conflict())),patch('backprop.foundry.time.sleep'):
            with self.assertRaises(BackpropError) as caught:journal.flush()
            self.assertIsInstance(caught.exception.__cause__,FoundryError)
            self.assertTrue(caught.exception.__cause__.retryable)
        self.assertEqual(journal.ack,0);self.assertFalse(journal.ack_path.exists())
        requests=[]
        def success(request,**kwargs):requests.append(request);return Response()
        with patch.object(self.store.opener,'open',side_effect=success):journal.flush()
        self.assertEqual(journal.ack,1)
        published=[json.loads(request.data) for request in requests if '/publishRecords' in request.full_url]
        self.assertEqual(len(published),1)
        self.assertEqual([record['event_id'] for record in published[0]['records']],['stable-id'])
        with patch.object(self.store.opener,'open',side_effect=AssertionError('already acknowledged')):journal.flush()


if __name__=='__main__':unittest.main()
