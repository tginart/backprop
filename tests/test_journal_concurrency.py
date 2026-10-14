"""Prove slow network uploads cannot prevent durable logging or duplicate acks.

Architecture: blocked fake publisher -> concurrent journal append/flush -> release
upload -> verify captured prefix, stable event IDs and persisted cursor recovery.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from backprop.transport import BackpropError, Journal, MemorySink, sync


class JournalConcurrencyTests(unittest.TestCase):
    def test_append_proceeds_during_upload_and_flush_has_a_fixed_prefix(self):
        entered, release, appended = threading.Event(), threading.Event(), threading.Event()
        errors = []

        class SlowSink(MemorySink):
            def publish(self, events):
                entered.set()
                if not release.wait(3): raise RuntimeError("test upload was never released")
                super().publish(events)

        with tempfile.TemporaryDirectory() as directory:
            sink = SlowSink()
            journal = Journal(directory, sink)
            journal.append({"event_id": "first"})
            def upload():
                try: journal.flush()
                except BaseException as error: errors.append(error)
            uploader = threading.Thread(target=upload)
            uploader.start()
            self.assertTrue(entered.wait(1))
            def append():
                journal.append({"event_id": "terminal"})
                appended.set()
            writer = threading.Thread(target=append)
            writer.start()
            try:
                self.assertTrue(appended.wait(1), "Network call held the append lock")
                self.assertEqual(len(Path(directory, "events.jsonl").read_text().splitlines()), 2)
            finally:
                release.set()
                uploader.join(3); writer.join(3)
            self.assertFalse(errors)
            self.assertEqual(journal.ack, 1)
            recovered = Journal(directory, sink)
            recovered.flush()
            self.assertEqual([event["event_id"] for event in sink.events], ["first", "terminal"])
            self.assertEqual(json.loads(Path(directory, "ack.json").read_text())["count"], 2)

    def test_deadline_retains_queue_without_sending_or_advancing_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = MemorySink(); journal = Journal(directory, sink)
            journal.append({"event_id": "one"})
            with self.assertRaises(TimeoutError): journal.flush(deadline=time.monotonic() - 1)
            self.assertEqual(journal.ack, 0)
            self.assertFalse(sink.events)
            journal.flush()
            self.assertEqual(journal.ack, 1)

    def test_concurrent_flushes_publish_each_event_once(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = MemorySink(); journal = Journal(directory, sink)
            for index in range(250): journal.append({"event_id": str(index)})
            workers = [threading.Thread(target=journal.flush) for _ in range(3)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(3)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual([event["event_id"] for event in sink.events], [str(i) for i in range(250)])

    def test_sync_wrong_directory_fails_without_creating_an_empty_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "wrong"
            with self.assertRaisesRegex(BackpropError, "sync_dir"): sync(missing, sink=MemorySink())
            self.assertFalse(missing.exists())

    def test_corrupted_staged_file_is_never_published_under_a_new_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = MemorySink(); journal = Journal(directory, sink)
            reference = journal.stage_file(None, data=b"original", name="model.bin")
            journal.append({"event_id": "immutable", "file": reference})
            Path(directory, reference["_backprop_local_file"]).write_bytes(b"changed")
            with self.assertRaisesRegex(BackpropError, "immutable SHA-256"):
                journal.flush()
            self.assertEqual(journal.ack, 0)
            self.assertFalse(sink.files); self.assertFalse(sink.events)

    def test_upload_race_cannot_replace_recorded_checksum(self):
        class ChangedUpload(MemorySink):
            def upload_file(self, path, name):
                Path(path).write_bytes(b"changed during upload")
                return super().upload_file(path, name)
        with tempfile.TemporaryDirectory() as directory:
            sink = ChangedUpload(); journal = Journal(directory, sink)
            reference = journal.stage_file(None, data=b"original", name="model.bin")
            journal.append({"event_id": "immutable", "file": reference})
            with self.assertRaisesRegex(BackpropError, "differs from the immutable"):
                journal.flush()
            self.assertEqual(journal.ack, 0); self.assertFalse(sink.events)


if __name__ == "__main__": unittest.main()
