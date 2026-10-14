"""Exercise storage boundaries that could silently lose or duplicate training data.

Architecture: deterministic HTTP doubles -> native FoundryStore methods ->
assert merged history, sparse-offset pagination, SQL safety, and upload isolation.
Live API verification is recorded separately by backend/verify.py.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backprop import foundry as MODULE

CONFIG = {"host": "https://foundry.example.com",
          "stream_dataset_rid": "ri.foundry.main.dataset.test-events",
          "media_dataset_rid": "ri.foundry.main.dataset.test-media"}


def event(identifier, kind="history"):
    return {"schema_version": 1, "event_id": identifier, "run_id": "run-1",
            "project": "project-1", "entity": "test", "type": kind,
            "timestamp": f"2026-10-02T01:00:{int(identifier):02d}+00:00",
            "payload": {"step": int(identifier), "metrics": {"loss": 0.1}}}


class NativeStorageTests(unittest.TestCase):
    def setUp(self):
        self.store = MODULE.FoundryStore(CONFIG, token="test-token")

    def test_workspace_revisions_never_enter_experiment_or_resume_reads(self):
        to_record = self.store.event_to_record
        layout = to_record(event("4", "workspace_snapshot"))
        layout["run_id"] = "__workspace__:test"
        # Exclude before decoding: malformed UI data must not break training.
        layout["payload"] = "not experiment JSON"
        with patch.object(self.store, "query_archive", return_value=[layout, to_record(event("1"))]), \
             patch.object(self.store, "read_live_records", return_value=[layout, to_record(event("2"))]):
            result = self.store.read_events()
        self.assertEqual([item["event_id"] for item in result], ["1", "2"])

    def test_merge_archive_and_live_deduplicates_overlapping_events(self):
        to_record = self.store.event_to_record
        with patch.object(self.store, "query_archive", return_value=[to_record(event("1")), to_record(event("2"))]), \
             patch.object(self.store, "read_live_records", return_value=[to_record(event("2")), to_record(event("3"))]):
            self.assertEqual([e["event_id"] for e in self.store.read_events()], ["1", "2", "3"])

    def test_archive_live_identity_conflict_fails_instead_of_overwriting(self):
        original=event("1")
        conflicting={**original,"payload":{"step":1,"metrics":{"loss":99}}}
        with patch.object(self.store,"query_archive",return_value=[self.store.event_to_record(original)]), \
             patch.object(self.store,"read_live_records",return_value=[self.store.event_to_record(conflicting)]):
            with self.assertRaisesRegex(ValueError,"Conflicting records"):
                self.store.read_events()

    def test_sparse_offsets_advance_past_last_actual_record(self):
        calls = []
        def respond(path, *args, **kwargs):
            calls.append(path)
            if "/getEndOffsets" in path:
                return {"0": "10"}
            if "startOffset=" not in path:
                return [{"offset": "0", "value": {"n": 0}}, {"offset": "3", "value": {"n": 3}}]
            if "startOffset=4" in path:
                return [{"offset": "9", "value": {"n": 9}}]
            raise AssertionError(path)
        with patch.object(self.store, "request", side_effect=respond):
            self.assertEqual(list(self.store.read_live_records()), [{"n": 0}, {"n": 3}, {"n": 9}])
        self.assertEqual(len(calls), 3)

    def test_truncated_page_retries_same_offset_and_preserves_sparse_snapshot(self):
        calls = []
        def respond(path, *args, **kwargs):
            from urllib.parse import parse_qs, urlsplit
            if '/getEndOffsets' in path:
                return {'0': '10'}
            query = parse_qs(urlsplit(path).query)
            offset, limit = query.get('startOffset', [None])[0], int(query['limit'][0])
            calls.append((offset, limit))
            if offset is None:
                return [{'offset': '3', 'value': {'n': 3}}]
            if limit > 25:
                raise MODULE.FoundryError('truncated', retryable=True, response_truncated=True)
            return [{'offset': '9', 'value': {'n': 9}}, {'offset': '10', 'value': {'n': 10}}]
        with patch.object(self.store, 'request', side_effect=respond):
            self.assertEqual(list(self.store.read_live_records()), [{'n': 3}, {'n': 9}])
        self.assertEqual(calls, [(None, 100), ('4', 100), ('4', 50), ('4', 25)])
        self.assertEqual(self.store._live_page_size, 25)

    def test_single_record_truncation_fails_without_skipping(self):
        sizes = []
        def respond(path, *args, **kwargs):
            from urllib.parse import parse_qs, urlsplit
            if '/getEndOffsets' in path:
                return {'0': '1'}
            query = parse_qs(urlsplit(path).query)
            self.assertNotIn('startOffset', query)
            sizes.append(int(query['limit'][0]))
            raise MODULE.FoundryError('truncated', retryable=True, response_truncated=True)
        with patch.object(self.store, 'request', side_effect=respond):
            with self.assertRaisesRegex(MODULE.FoundryError, 'truncated'):
                list(self.store.read_live_records())
        self.assertEqual(sizes, [100, 50, 25, 12, 6, 3, 1])

    def test_other_failures_do_not_resize_or_skip_pages(self):
        for error in (MODULE.FoundryError('timeout', retryable=True), MODULE.FoundryError('denied', status_code=403)):
            with patch.object(self.store, 'request', side_effect=[{'0': '1'}, error]) as request:
                with self.assertRaises(MODULE.FoundryError) as raised:
                    list(self.store.read_live_records())
            self.assertIs(raised.exception, error)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(self.store._live_page_size, 100)

    def test_exhausted_http_truncation_is_classified_without_partial_data(self):
        from unittest.mock import MagicMock
        import http.client
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = http.client.IncompleteRead(b'partial', 20)
        with patch.object(self.store.opener, 'open', return_value=response) as opened, patch.object(MODULE.time, 'sleep'):
            with self.assertRaises(MODULE.FoundryError) as raised:
                self.store.request('/read', retry=True)
        self.assertEqual(opened.call_count, 4)
        self.assertTrue(raised.exception.response_truncated)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn('partial', str(raised.exception))

    def test_records_beyond_initial_end_are_not_included(self):
        with patch.object(self.store, "request", side_effect=[{"0": "1"},
                [{"offset": "0", "value": {"n": 0}}, {"offset": "1", "value": {"n": 1}}]]):
            self.assertEqual(list(self.store.read_live_records()), [{"n": 0}])

    def test_archive_failure_cannot_look_like_empty_history(self):
        with patch.object(self.store, "query_archive", side_effect=MODULE.FoundryError("denied")):
            with self.assertRaisesRegex(MODULE.FoundryError, "denied"):
                self.store.read_events()

    def test_project_string_never_enters_sql_syntax(self):
        capture = []
        def respond(path, method="GET", body=None, **kwargs):
            if path.endswith("/execute"):
                capture.append(body["query"])
                return {"type": "succeeded", "queryId": "query/id"}
            return b"schema_version,event_id,run_id,project,entity,event_type,timestamp,payload\n"
        malicious = "x' OR 1=1 --"
        with patch.object(self.store, "request", side_effect=respond):
            self.assertEqual(self.store.query_archive(project=malicious), [])
        self.assertNotIn(malicious, capture[0])
        self.assertIn(malicious.encode().hex(), capture[0])

    def test_large_event_rejected_before_network_call(self):
        large = event("1")
        large["payload"] = {"table": "a" * 900_001}
        with patch.object(self.store, "request") as transport:
            with self.assertRaisesRegex(ValueError, "900 KB"):
                self.store.publish([large])
            transport.assert_not_called()

    def test_uploaded_filename_cannot_escape_content_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.txt"
            path.write_text("deterministic content")
            with patch.object(self.store, "request"):
                result = self.store.upload_file(path, "../../../elsewhere/file.txt")
            self.assertEqual(result["path"].split("/")[0], "blobs")
            self.assertEqual(result["path"].split("/")[-1], "file.txt")
            self.assertNotIn("..", result["path"])

    def test_host_and_relative_path_protect_token_destination(self):
        with self.assertRaises(ValueError):
            MODULE.FoundryStore({**CONFIG, "host": "https://trusted.example@evil.example"}, "token")
        with self.assertRaises(ValueError):
            self.store.request("//evil.example/api")


if __name__ == "__main__":
    unittest.main()
