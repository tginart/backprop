"""Keep nested media uploads inside the journal's durable event boundary.

Architecture: immutable parent/child file snapshots -> recoverable upload ->
publish only fully resolved events. Main callers: image masks and media nested
in other rich values. No array or image dependency is needed for these tests.
"""
import json
from pathlib import Path
import tempfile
import unittest

from backprop.transport import BackpropError, Journal, MemorySink, read_journal


class RecordingSink(MemorySink):
    def __init__(self):
        super().__init__()
        self.uploads=[]
        self.fail_child=False
        self.fail_publish=False

    def upload_file(self,path,name):
        self.uploads.append(name)
        result=super().upload_file(path,name)
        if self.fail_child and name.endswith('/mask.png'):
            self.fail_child=False
            raise OSError('lost child upload acknowledgement')
        return result

    def publish(self,events):
        super().publish(events)
        if self.fail_publish:
            self.fail_publish=False
            raise OSError('lost event acknowledgement')


class NestedMediaTransportTests(unittest.TestCase):
    def fixture(self,directory,sink):
        journal=Journal(directory,sink)
        child=journal.stage_file(None,data=b'mask pixels',name='mask.png')
        parent=journal.stage_file(None,data=b'image pixels',name='image.png')
        descriptor={**parent,'_type':'image','masks':{'prediction':{'mask':child,'class_labels':{'255':'foreground'}}}}
        event={'event_id':'nested-image','image':descriptor}
        journal.append(event)
        return journal,child,event

    def assert_resolved(self,sink):
        for event in sink.events:
            self.assertNotIn('_backprop_local_file',json.dumps(event))
            self.assertNotIn('_backprop_live_media',json.dumps(event))
            parent=event['image']; child=parent['masks']['prediction']['mask']
            self.assertEqual(sink.download_file(parent),b'image pixels')
            self.assertEqual(sink.download_file(child),b'mask pixels')
            self.assertEqual(child['mime_type'],'image/png')
            self.assertEqual(parent['masks']['prediction']['class_labels'],{'255':'foreground'})

    def test_nested_files_resolve_without_mutating_the_immutable_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=RecordingSink(); journal,child,event=self.fixture(directory,sink)
            journal.flush(); self.assert_resolved(sink)
            self.assertEqual(journal.ack,1)
            self.assertEqual(len(sink.uploads),2)
            self.assertEqual(read_journal(directory),[event])

    def test_missing_or_corrupt_child_prevents_publication_and_recovers(self):
        for kind in ['missing','corrupt']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                sink=RecordingSink(); journal,child,_=self.fixture(directory,sink)
                path=Path(directory,child['_backprop_local_file'])
                if kind=='missing':path.unlink()
                else:path.write_bytes(b'corrupt')
                with self.assertRaises(BackpropError):journal.flush()
                self.assertEqual(journal.ack,0); self.assertFalse(sink.events)
                self.assertFalse(journal.ack_path.exists())
                path.write_bytes(b'mask pixels')
                recovered=Journal(directory,sink); recovered.flush()
                self.assertEqual(recovered.ack,1); self.assert_resolved(sink)

    def test_failed_child_upload_retries_using_the_same_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=RecordingSink(); sink.fail_child=True
            journal,child,_=self.fixture(directory,sink)
            with self.assertRaisesRegex(BackpropError,'lost child'):journal.flush()
            self.assertEqual(journal.ack,0); self.assertFalse(sink.events)
            journal.flush(); self.assert_resolved(sink)
            self.assertEqual(len(sink.files),2)
            self.assertEqual(sink.uploads[1],sink.uploads[2])
            self.assertEqual(len(sink.uploads),3)

    def test_restart_after_lost_event_ack_preserves_ids_and_nested_references(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=RecordingSink(); sink.fail_publish=True
            journal,_,_=self.fixture(directory,sink)
            with self.assertRaisesRegex(BackpropError,'lost event'):journal.flush()
            self.assertEqual(journal.ack,0)
            recovered=Journal(directory,sink); recovered.flush()
            self.assertEqual(recovered.ack,1); self.assert_resolved(sink)
            self.assertEqual(sink.events[0],sink.events[1])
            self.assertEqual(len(sink.files),2)
            self.assertEqual(sink.uploads[:2],sink.uploads[2:])
            final=Journal(directory,sink); final.flush()
            self.assertEqual(len(sink.events),2)


if __name__=='__main__':unittest.main()
