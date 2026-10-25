"""Exercise storage boundaries that could silently lose or duplicate training data.

Architecture: deterministic HTTP doubles -> native FoundryStore methods ->
assert merged history, sparse-offset pagination, SQL safety, and upload isolation.
Live API verification is recorded separately by backend/verify.py.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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

    def test_large_live_snapshot_uses_bounded_network_round_trips(self):
        """
        Purpose: Keep a realistic stream snapshot inside the default startup budget.
        Input: A 38,900-record stream with sparse-offset-compatible paging.
        Output: All records with at most forty live page requests.
        """
        from urllib.parse import parse_qs, urlsplit
        calls = []
        count = 38_900
        def respond(path, *args, **kwargs):
            if '/getEndOffsets' in path:
                return {'0': str(count)}
            query = parse_qs(urlsplit(path).query)
            start = int(query.get('startOffset', ['0'])[0])
            limit = int(query['limit'][0])
            calls.append(start)
            return [{'offset': str(index), 'value': {'n': index}}
                    for index in range(start, min(start + limit, count))]
        with patch.object(self.store, 'request', side_effect=respond):
            rows = list(self.store.read_live_records())
        self.assertEqual([row['n'] for row in rows], list(range(count)))
        self.assertLessEqual(len(calls), 40)

    def test_prefetch_uses_bounded_concurrent_reads(self):
        """
        Purpose: Prevent sequential network latency from exhausting the startup budget.
        Input: Four independent pages that must overlap before they can complete.
        Output: A complete ordered snapshot with four concurrent requests.
        """
        from threading import Barrier
        from urllib.parse import parse_qs, urlsplit
        barrier = Barrier(4)
        def respond(path, *args, **kwargs):
            if '/getEndOffsets' in path:
                return {'0': '5000'}
            query = parse_qs(urlsplit(path).query)
            start = int(query.get('startOffset', ['0'])[0])
            if start:
                barrier.wait(timeout=3)
            return [{'offset': str(value), 'value': {'n': value}}
                    for value in range(start, start + 1000)]
        with patch.object(self.store, 'request', side_effect=respond):
            rows = list(self.store.read_live_records())
        self.assertEqual([row['n'] for row in rows], list(range(5000)))

    def test_parallel_prefetch_cannot_skip_valid_short_or_sparse_pages(self):
        """
        Purpose: Reject offset guesses as evidence that a prefix was consumed.
        Input: Short pages, sparse offsets, overlapping prefetch, and a fixed end.
        Output: Each snapshot record once in offset order.
        """
        from urllib.parse import parse_qs, urlsplit
        values = [0, 1, 2, 4, 7, 8, 12, 19, 20, 29, 41, 59]
        self.store._live_page_size = 10
        def respond(path, *args, **kwargs):
            if '/getEndOffsets' in path:
                return {'0': '60'}
            query = parse_qs(urlsplit(path).query)
            start = int(query.get('startOffset', ['0'])[0])
            return [{'offset': str(value), 'value': {'n': value}}
                    for value in [value for value in values if value >= start][:2]]
        with patch.object(self.store, 'request', side_effect=respond):
            rows = list(self.store.read_live_records())
        self.assertEqual([row['n'] for row in rows], values)

    def test_truncated_page_retries_same_offset_and_preserves_sparse_snapshot(self):
        self.store._live_page_size = 100
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
        self.store._live_page_size = 100
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
        self.store._live_page_size = 100
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

    def test_compressed_responses_preserve_decoded_json_and_retry_corrupt_bodies(self):
        """
        Purpose: Reduce Foundry transfer size without a different retry boundary.
        Input: A gzip JSON response and a truncated gzip response.
        Output: Exact JSON or a retryable truncated-response error after four attempts.
        """
        import gzip
        payload = gzip.compress(b'{"value": "compressed"}')
        def response(data):
            value = MagicMock()
            value.__enter__.return_value = value
            value.headers = {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}
            value.read.return_value = data
            return value
        with patch.object(self.store.opener, 'open', return_value=response(payload)) as opened:
            self.assertEqual(self.store.request('/api/example'), {'value': 'compressed'})
            self.assertEqual(opened.call_args.args[0].get_header('Accept-encoding'), 'gzip')
        with patch.object(self.store.opener, 'open', side_effect=lambda *args, **kwargs: response(payload[:-4])) as opened, \
                patch.object(MODULE.time, 'sleep'):
            with self.assertRaises(MODULE.FoundryError) as raised:
                self.store.request('/api/example', retry=True)
            self.assertEqual(opened.call_count, 4)
            self.assertTrue(raised.exception.response_truncated)

    def test_compressed_error_body_preserves_status_name_and_credential_redaction(self):
        """
        Purpose: Preserve Foundry duplicate and authorization decisions with gzip diagnostics.
        Input: A compressed structured HTTP error and a corrupt compressed denial.
        Output: The original status and error name without a credential leak or false retry.
        """
        import gzip
        import io
        from urllib.error import HTTPError
        body = gzip.compress(b'{"errorName":"FileAlreadyExists","message":"test-token"}')
        error = HTTPError('https://example.test/api', 404, 'duplicate',
                          {'Content-Encoding': 'gzip'}, io.BytesIO(body))
        with patch.object(self.store.opener, 'open', side_effect=error):
            with self.assertRaises(MODULE.FoundryError) as raised:
                self.store.request('/api/example')
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.error_name, 'FileAlreadyExists')
        self.assertNotIn('test-token', str(raised.exception))
        error = HTTPError('https://example.test/api', 403, 'denied',
                          {'Content-Encoding': 'gzip'}, io.BytesIO(body[:-4]))
        with patch.object(self.store.opener, 'open', side_effect=error) as opened:
            with self.assertRaises(MODULE.FoundryError) as denied:
                self.store.request('/api/example', retry=True)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertFalse(denied.exception.retryable)

    def test_gzip_transient_errors_replay_the_original_request_body(self) -> None:
        """
        Purpose: Retain the original payload across compressed HTTP error retries.
        Input: Read, JSON publication, and binary upload requests with a gzip 503.
        Output: The retry sends the same method and bytes, including an absent body.
        """
        import gzip
        import io
        from urllib.error import HTTPError
        for method, payload, expected in (
                ('GET', None, None),
                ('POST', {'records': [1]}, b'{"records":[1]}'),
                ('POST', b'binary artifact', b'binary artifact')):
            with self.subTest(method=method, payload=payload):
                attempts = []

                def respond(req, **options):
                    attempts.append((req.get_method(), req.data))
                    if len(attempts) == 1:
                        raise HTTPError(req.full_url, 503, 'unavailable',
                                        {'Content-Encoding': 'gzip'},
                                        io.BytesIO(gzip.compress(b'transient')))
                    self.assertEqual(req.data, expected)
                    result = MagicMock()
                    result.__enter__.return_value = result
                    result.headers = {'Content-Type': 'application/json'}
                    result.read.return_value = b'{"ok":true}'
                    return result

                with patch.object(self.store.opener, 'open', side_effect=respond), \
                        patch.object(MODULE.time, 'sleep'):
                    self.assertEqual(self.store.request('/retry', method, payload,
                                                        retry=True), {'ok': True})
                self.assertEqual(attempts, [(method, expected)] * 2)

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
