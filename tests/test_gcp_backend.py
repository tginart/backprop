"""
Purpose: Verify native Firestore transactions and the public acceptance scenario.
Input: Google's Firestore emulator, a bucket fixture, and real HTTP requests.
Output: Evidence for concurrent writers, restart, retries, Google isolation, and SDK parity.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from urllib import request

try:
    from google.api_core.exceptions import NotFound, PreconditionFailed
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
except ImportError:
    raise unittest.SkipTest("Install the gcp extra for Firestore contract coverage")

if not os.environ.get("FIRESTORE_EMULATOR_HOST", "").startswith(("127.0.0.1:", "localhost:")):
    raise unittest.SkipTest("Run tools/firestore_emulator.py for native Firestore contract tests")

from backend_acceptance import run_scenario
from backprop_backend.connection import create_store, snapshot_store
from backprop_backend.gcp.auth import GoogleAuthorizer
from backprop_backend.gcp.config import STREAM_RID, normalize_gcp_config
from backprop_backend.gcp.database import FirestoreDatabase
from backprop_backend.gcp.server import create_server
from backprop_backend.http_store import BackendError, HttpStore
from backprop_backend.local.database import LocalConflict
from test_local_backend import event


ORIGIN = "https://gcp.example"
TOKEN = "synthetic-google-access-token"
EMAIL = "operator@example.com"


class Bucket:
    """
    Purpose: Model strong GCS object consistency and generation preconditions.
    Input: Independent blob handles from concurrent callers.
    Output: Atomic object replacements or the native precondition exception.
    """

    def __init__(self):
        self.name = "fixture-" + uuid.uuid4().hex
        self.objects = {}
        self.lock = threading.Lock()
        self.generation = 0
        self.conflicts = 0

    def blob(self, name):
        return Blob(self, name)


class Blob:
    """One GCS object handle with a separately observed generation."""

    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.generation = None

    def reload(self, **options):
        with self.bucket.lock:
            if self.name not in self.bucket.objects:
                raise NotFound("absent")
            self.generation = self.bucket.objects[self.name][0]

    def download_as_bytes(self, *, if_generation_match=None, **options):
        with self.bucket.lock:
            if self.name not in self.bucket.objects:
                raise NotFound("absent")
            generation, data = self.bucket.objects[self.name]
            if if_generation_match is not None and generation != if_generation_match:
                self.bucket.conflicts += 1
                raise PreconditionFailed("changed")
            return data

    def upload_from_string(self, data, *, if_generation_match, **options):
        with self.bucket.lock:
            generation = self.bucket.objects.get(self.name, (0, None))[0]
            if generation != if_generation_match:
                self.bucket.conflicts += 1
                raise PreconditionFailed("changed")
            self.bucket.generation += 1
            self.generation = self.bucket.generation
            self.bucket.objects[self.name] = (self.generation, bytes(data))

    def upload_from_file(self, source, **options):
        self.upload_from_string(source.read(), **options)

    @contextmanager
    def open(self, mode, **options):
        with io.BytesIO() as stream:
            if mode == "rb":
                stream.write(self.download_as_bytes())
                stream.seek(0)
                yield stream
            else:
                assert mode == "wb"
                yield stream
                self.upload_from_string(stream.getvalue(), **options)

    def delete(self, **options):
        with self.bucket.lock:
            if self.name not in self.bucket.objects:
                raise NotFound("absent")
            del self.bucket.objects[self.name]


def firestore_client():
    return firestore.Client(project="demo-backprop-tests", credentials=AnonymousCredentials())


def all_records(database):
    return [row for partition in database.end_offsets()
            for row in database.read_records(start=0, limit=1000, partition=partition)]


@contextmanager
def service(bucket=None):
    """
    Purpose: Exercise real HTTP while the production origin remains HTTPS-only.
    Input: An optional persistent generation-aware bucket.
    Output: A GCP server and a test-only loopback transport.
    """
    client = firestore_client()
    database = FirestoreDatabase(client, bucket or Bucket())
    authorize = GoogleAuthorizer([EMAIL], verify_token=lambda token:
        {"email": EMAIL, "email_verified": "true", "expires_in": 3600}
        if token == TOKEN else {})
    server = create_server(database=database, origin=ORIGIN, authorize=authorize,
                           host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = request.build_opener(request.ProxyHandler({}))

    class TestOpener:
        def open(self, req, **options):
            target = request.Request(
                f"http://127.0.0.1:{server.server_port}" + req.full_url[len(ORIGIN):],
                data=req.data, method=req.get_method(), headers=dict(req.headers))
            return opener.open(target, **options)

    try:
        with patch("backprop_backend.gcp.store.request.build_opener", return_value=TestOpener()):
            yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        client.close()


class GcpDatabaseTests(unittest.TestCase):
    """Verify the durable shared storage contract across independent instances."""

    def setUp(self):
        self.bucket = Bucket()
        self.client = firestore_client()
        self.database = FirestoreDatabase(self.client, self.bucket)

    def tearDown(self):
        self.client.close()

    def test_concurrent_writers_restart_and_conflicting_batch_are_atomic(self):
        instances = [FirestoreDatabase(self.client, self.bucket) for _ in range(4)]
        records = [HttpStore.event_to_record(event(f"event-{i}")) for i in range(24)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda pair: pair[0].publish([pair[1]]),
                          [(instances[i % 4], row) for i, row in enumerate(records)]))
        restored = FirestoreDatabase(self.client, self.bucket)
        self.assertEqual(restored.installation_id, self.database.installation_id)
        self.assertEqual(restored.end_offset(), 24)
        restored.publish(records)
        self.assertEqual(restored.end_offset(), 24)
        with self.assertRaises(LocalConflict):
            restored.publish([HttpStore.event_to_record(event("new")),
                              {**records[0], "run_id": "conflict"}])
        self.assertEqual(restored.end_offset(), 24)
        self.assertEqual(len(all_records(restored)), 24)
        for partition, end in restored.end_offsets().items():
            self.assertEqual([row["offset"] for row in restored.read_records(
                start=0, limit=100, partition=partition)], [str(i) for i in range(int(end))])

    def test_history_boundary_query_restart_cancel_and_expiry(self):
        self.database.publish([HttpStore.event_to_record(event("first"))])
        query = f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100"
        identifier = self.database.execute_query(query)
        self.database.publish([HttpStore.event_to_record(event("second"))])
        restored = FirestoreDatabase(self.client, self.bucket)
        result, boundary = restored.query_page(identifier)
        self.assertEqual(sum(map(int, boundary.values())), 1)
        self.assertIn(b"first", result)
        self.assertNotIn(b"second", result)
        restored.cancel_query(identifier)
        restored.cancel_query(identifier)
        self.assertFalse(restored.has_query(identifier))
        with self.assertRaises(FileNotFoundError):
            restored.query_page(identifier)
        for query in ("DROP TABLE events", query.replace(STREAM_RID, "foreign")):
            with self.assertRaises(ValueError):
                restored.execute_query(query)

    def test_failed_history_export_does_not_publish_a_partial_result(self):
        """
        Purpose: Abort the GCS result when a Firestore history read fails.
        Input: A history iterator that fails after one valid record.
        Output: No query descriptor or finalized partial CSV object.
        """
        record = HttpStore.event_to_record(event("partial"))

        def interrupted(plan, read_time):
            yield record
            raise OSError("history read interrupted")

        query = f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100"
        with patch.object(self.database, "_history_records", interrupted):
            with self.assertRaisesRegex(OSError, "history read interrupted"):
                self.database.execute_query(query)
        self.assertEqual(self.bucket.objects, {})
        self.assertEqual(list(self.database.root.collection("queries").stream()), [])

    def test_artifacts_integrity_and_concurrent_append_claims(self):
        data = b"immutable artifact"
        path = f"blobs/{hashlib.sha256(data).hexdigest()}/weights.bin"
        self.database.upload_file(path, data, append=False)
        self.assertEqual(FirestoreDatabase(self.client, self.bucket).read_file(path), data)
        with self.assertRaises(ValueError):
            self.database.upload_file(path, b"wrong", append=False)
        claim = "artifact-resolutions/" + "a" * 64 + ".json"

        def compete(i):
            try:
                FirestoreDatabase(self.client, self.bucket).upload_file(claim, json.dumps({"winner": i}).encode(), append=True)
                return i
            except FileExistsError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = [i for i in pool.map(compete, range(8)) if i is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(json.loads(self.database.read_file(claim)), {"winner": winners[0]})

    def test_prefixes_isolate_installations(self):
        isolated = FirestoreDatabase(self.client, self.bucket, prefix="other")
        self.assertNotEqual(isolated.installation_id, self.database.installation_id)
        self.database.publish([HttpStore.event_to_record(event("private"))])
        self.assertEqual(isolated.end_offset(), 0)

    def test_metrics_never_transfer_a_gcs_database_and_commit_only_new_records(self):
        """
        Purpose: Verify bounded write work independently of stored history size.
        Input: A new event before and after a larger history exists.
        Output: Two Firestore writes per event and no GCS metrics traffic.
        """
        api = self.client._firestore_api
        with patch.object(api, "commit", wraps=api.commit) as commit:
            self.database.publish([HttpStore.event_to_record(event("first"))])
        self.assertEqual(len(commit.call_args.kwargs["request"]["writes"]), 2)
        self.database.publish([HttpStore.event_to_record(event(f"history-{i}")) for i in range(500)])
        with patch.object(api, "commit", wraps=api.commit) as commit:
            self.database.publish([HttpStore.event_to_record(event("last"))])
        self.assertEqual(len(commit.call_args.kwargs["request"]["writes"]), 2)
        self.assertEqual(self.bucket.objects, {})
        self.assertEqual(self.database.end_offset(), 502)

    def test_query_expiry_and_corrupt_artifacts_fail_explicitly(self):
        query = f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100"
        with patch("backprop_backend.gcp.database.time") as clock:
            clock.time.return_value = 10000
            identifier = self.database.execute_query(query)
            clock.time.return_value = 13600
            self.assertFalse(self.database.has_query(identifier))
        data = b"integrity"
        digest = hashlib.sha256(data).hexdigest()
        path = f"blobs/{digest}/weights"
        self.database.upload_file(path, data, append=False)
        name = self.database.prefix + "/artifacts/" + digest
        self.bucket.objects[name] = (self.bucket.objects[name][0], b"corrupt")
        with self.assertRaises(OSError):
            self.database.read_file(path)

    def test_query_snapshot_excludes_a_concurrent_backdated_write(self):
        """
        Purpose: Keep archive rows and cursor vectors at the same commit boundary.
        Input: A write after snapshot capture but before native query reads.
        Output: An immutable old snapshot and the new event in incremental reads.
        """
        first = HttpStore.event_to_record(event("before"))
        self.database.publish([first])
        original = self.database._history_records

        def concurrent(plan, read_time):
            self.database.publish([{**first, "event_id": "after", "timestamp": "2020-01-01T00:00:00Z"}])
            return original(plan, read_time)

        with patch.object(self.database, "_history_records", concurrent):
            identifier = self.database.execute_query(
                f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100")
        data, boundaries = self.database.query_page(identifier)
        self.assertEqual([row["event_id"] for row in csv.DictReader(io.StringIO(data.decode()))], ["before"])
        self.assertEqual(sum(map(int, boundaries.values())), 1)
        tail = [row["value"]["event_id"] for partition, start in boundaries.items()
                for row in self.database.read_records(start=int(start), limit=100, partition=partition)]
        self.assertEqual(tail, ["after"])

    def test_long_unicode_identities_scope_filters_and_exact_cursor_pages(self):
        """
        Purpose: Preserve scope equality and cursor order beyond Firestore index limits.
        Input: Long Unicode IDs with identical indexed prefixes and encoded scope values.
        Output: Complete ordered pages without overlap, truncation, or foreign records.
        """
        # Python 3.10 CSV rejects NUL fields in both existing backends. Use a
        # newline here to exercise CSV escapes on every supported Python version.
        scope = "项目/quote'\nscope"
        identifiers = ["长" * 1000 + f"-{index:03d}" for index in range(20)]
        records = [HttpStore.event_to_record(event(identifier)) for identifier in identifiers]
        records = [{**record, "project": scope, "timestamp": "2026-01-01T00:00:00Z"}
                   for record in records]
        self.database.publish(records)
        self.database.publish([{**records[0], "event_id": "foreign", "project": "other"}])
        found, cursor = [], None
        while True:
            clauses = ["project = " + HttpStore._sql_text(scope)]
            if cursor is not None:
                stamp, identifier = map(HttpStore._sql_text, cursor)
                clauses.append(f"(timestamp < {stamp} OR (timestamp = {stamp} AND event_id < {identifier}))")
            query = f"SELECT * FROM `{STREAM_RID}` WHERE " + " AND ".join(clauses)
            query += " ORDER BY timestamp DESC, event_id DESC LIMIT 3"
            identifier = self.database.execute_query(query)
            rows = list(csv.DictReader(io.StringIO(self.database.query_result(identifier).decode())))
            found.extend(row["event_id"] for row in rows)
            if len(rows) < 3:
                break
            cursor = rows[-1]["timestamp"], rows[-1]["event_id"]
        self.assertEqual(found, sorted(identifiers, reverse=True))

    def test_native_stream_preserves_null_characters_in_identity_fields(self):
        record = HttpStore.event_to_record(event("null-scope"))
        record["project"] = "scope\x00value"
        self.database.publish([record])
        self.assertEqual(
            [HttpStore.record_to_event(row["value"]) for row in all_records(self.database)],
            [HttpStore.record_to_event(record)],
        )

    def test_canonical_duplicates_and_conflicts_within_one_batch(self):
        record = HttpStore.event_to_record(event("same"))
        equivalent = {**record, "payload": json.dumps(json.loads(record["payload"]), indent=4)}
        self.database.publish([record, equivalent])
        self.assertEqual(self.database.end_offset(), 1)
        with self.assertRaises(LocalConflict):
            self.database.publish([HttpStore.event_to_record(event("new")),
                                   {**record, "run_id": "different"}, record])
        self.assertEqual(self.database.end_offset(), 1)

    def test_invalid_utf8_identity_rejects_the_whole_batch_before_a_transaction(self):
        record = HttpStore.event_to_record(event("invalid"))
        for field in ("event_id", "project", "entity", "run_id", "event_type", "timestamp"):
            with self.subTest(field=field), \
                    patch.object(self.client, "transaction", wraps=self.client.transaction) as transaction, \
                    self.assertRaises(UnicodeError):
                self.database.publish([HttpStore.event_to_record(event("valid")),
                                       {**record, field: "invalid-\ud800"}])
            transaction.assert_not_called()
        self.assertEqual(self.database.end_offset(), 0)

    def test_large_payloads_and_deep_json_are_unindexed_record_bytes(self):
        record = HttpStore.event_to_record(event("large"))
        nested = {"value": 1}
        for _ in range(40):
            nested = {"nested": nested}
        record["payload"] = json.dumps({"large": "x" * 850000, "deep": nested})
        self.database.publish([record])
        observed = all_records(self.database)[0]["value"]
        self.assertEqual(json.loads(observed["payload"]), json.loads(record["payload"]))
        self.assertEqual(self.bucket.objects, {})

    def test_firestore_storage_operations_never_open_sqlite(self):
        with patch("sqlite3.connect", side_effect=AssertionError("SQLite must not serve GCP")):
            self.database.publish([HttpStore.event_to_record(event("native"))])
            identifier = self.database.execute_query(
                f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100")
            self.assertIn(b"native", self.database.query_result(identifier))
            data = b"artifact"
            path = f"blobs/{hashlib.sha256(data).hexdigest()}/native"
            self.database.upload_file(path, data, append=False)
            self.assertEqual(self.database.read_file(path), data)


class GcpHttpTests(unittest.TestCase):
    """Run SDK acceptance with the real HTTP contract and a GCS fixture."""

    def test_existing_shared_acceptance_scenario(self):
        with service() as server, patch.dict("os.environ", {"BACKPROP_GCP_ACCESS_TOKEN": TOKEN}):
            result = run_scenario(server.connection, token=None, archive_timeout_s=5)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(result["checks"]), 11)

    def test_exhausted_firestore_contention_remains_retryable(self):
        from google.api_core.exceptions import Aborted
        from backprop.failures import retryable_failure

        exhausted = ValueError("Transaction retries exhausted")
        exhausted.__cause__ = Aborted("Contention")
        with service() as server:
            store = create_store(server.connection, token=TOKEN)
            with patch("backprop_backend.gcp.database.firestore.transactional",
                       return_value=Mock(side_effect=exhausted)), \
                    patch("backprop_backend.http_store.time.sleep"), \
                    self.assertRaises(BackendError) as caught:
                store.publish([event("retry")])
            self.assertEqual(caught.exception.status_code, 503)
            self.assertTrue(retryable_failure(caught.exception))
            self.assertEqual(server.database.end_offset(), 0)

    def test_auth_origin_redirect_isolation_and_public_assets(self):
        with service() as server, patch.dict("os.environ", {
            "BACKPROP_TOKEN": "wrong-foundry", "FOUNDRY_TOKEN": "wrong-foundry",
            "BACKPROP_GCP_ACCESS_TOKEN": TOKEN,
        }):
            store = create_store(server.connection)
            self.assertEqual(store.token, TOKEN)
            self.assertEqual(store.request("/api/backprop/identity")["displayName"], EMAIL)
            self.assertEqual(snapshot_store(store)["token"], TOKEN)
            denied = create_store(server.connection, token="unauthorized")
            with self.assertRaises(BackendError) as caught:
                denied.request(store.stream_path + "/getEndOffsets")
            self.assertEqual(caught.exception.status_code, 403)
            with self.assertRaises(BackendError) as encoded:
                denied.request(store.stream_path.replace('/api/', '/%61pi/', 1) + '/getEndOffsets')
            self.assertEqual(encoded.exception.status_code, 403)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertIn("gcp-", store.ack_namespace)
            with self.assertRaises(ValueError):
                create_store({**server.connection, "gcp_id": "f" * 32}, token=TOKEN)

    def test_google_claims_and_unsafe_connections_fail_closed(self):
        for claims in ({}, {"email": EMAIL, "verified_email": False, "expires_in": 60},
                       {"email": EMAIL, "verified_email": True, "expires_in": 0},
                       {"email": "foreign@example.com", "verified_email": True, "expires_in": 60}):
            auth = GoogleAuthorizer([EMAIL], verify_token=lambda token: claims)
            with self.assertRaises(PermissionError):
                auth("Bearer synthetic")
        for host in ("http://localhost", "https://gcp.example/path", "https://user@gcp.example",
                     "https://gcp.example?query=x", "https://gcp.example:8443"):
            with self.assertRaises(ValueError):
                normalize_gcp_config({"backend": "gcp", "host": host, "gcp_id": "a" * 32})

    def test_google_verification_outages_remain_retryable_and_deny_writes(self):
        import requests
        from backprop.failures import retryable_failure

        outages = [Mock(status_code=status) for status in (408, 429, 500, 502, 503, 504)]
        outages += [requests.Timeout(TOKEN), requests.ConnectionError(TOKEN)]
        malformed = Mock(status_code=200)
        malformed.json.side_effect = ValueError(TOKEN)
        outages.append(malformed)
        with service() as server:
            store = create_store(server.connection, token=TOKEN)
            server.authorize = GoogleAuthorizer([EMAIL])
            for outage in outages:
                options = {"side_effect": outage} if isinstance(outage, Exception) else {"return_value": outage}
                with self.subTest(outage=type(outage).__name__), \
                        patch("requests.get", **options) as verify, \
                        patch("backprop_backend.http_store.time.sleep"), \
                        self.assertRaises(BackendError) as caught:
                    store.publish([event("unverified")])
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.error_name, "AuthenticationUnavailable")
                self.assertTrue(retryable_failure(caught.exception))
                self.assertEqual(verify.call_count, 4)
                self.assertEqual(server.database.end_offset(), 0)
                self.assertNotIn(TOKEN, str(caught.exception))
            for status in (400, 401, 403):
                with patch("requests.get", return_value=Mock(status_code=status)) as verify, \
                        self.assertRaises(BackendError) as caught:
                    store.publish([event("denied")])
                self.assertEqual(caught.exception.status_code, 403)
                self.assertFalse(retryable_failure(caught.exception))
                self.assertEqual(verify.call_count, 1)

    def test_sdk_background_upload_recovers_after_google_verification_outage(self):
        import backprop

        with service() as server, tempfile.TemporaryDirectory() as directory:
            store = create_store(server.connection, token=TOKEN)
            run = backprop.init(project="google-outage", dir=directory, _sink=store,
                _deployment=server.connection, settings={"console": "off", "silent": True,
                    "disable_git": True, "x_disable_stats": True, "flush_interval": 0.02})
            try:
                run.flush()
                server.authorize = GoogleAuthorizer([EMAIL])
                allowed = Mock(status_code=200)
                allowed.json.return_value = {"email": EMAIL, "email_verified": True, "expires_in": 3600}
                responses = [Mock(status_code=503) for _ in range(4)]

                def verify(*args, **options):
                    return responses.pop() if responses else allowed

                with patch("requests.get", side_effect=verify), \
                        patch("backprop_backend.http_store.time.sleep"):
                    run.log({"recovered": 7}, commit=True)
                    deadline = time.monotonic() + 5
                    while True:
                        records = all_records(server.database)
                        if any(json.loads(row["value"]["payload"]).get("metrics", {}).get("recovered") == 7
                               for row in records):
                            break
                        self.assertLess(time.monotonic(), deadline, "The background uploader did not recover")
                        threading.Event().wait(0.02)
                    self.assertEqual(responses, [])
                    self.assertTrue(run._thread.is_alive())
            finally:
                server.authorize = GoogleAuthorizer([EMAIL], verify_token=lambda token:
                    {"email": EMAIL, "email_verified": True, "expires_in": 3600})
                run.finish()
                backprop.teardown()

    def test_large_chunked_upload_download_and_incomplete_upload(self):
        with service() as server, tempfile.TemporaryDirectory() as directory:
            store = create_store(server.connection, token=TOKEN)
            source = Path(directory) / "large.bin"
            data = b"large-artifact" * 700000
            source.write_bytes(data)
            reference = store.upload_file(source)
            self.assertEqual(store.download_file(reference), data)
            ranged = request.Request(store.file_url(reference), headers={
                "Authorization": "Bearer " + TOKEN, "Range": "bytes=100-199",
            })
            with store.opener.open(ranged, timeout=10) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Range"], f"bytes 100-199/{len(data)}")
                self.assertEqual(response.read(), data[100:200])
            store.upload_file(source)
            digest = "f" * 64
            with self.assertRaises(BackendError):
                store.request(f"/api/backprop/uploads/{digest}/complete", "POST", {
                    "path": "blobs/" + digest + "/absent", "size_bytes": 10,
                    "part_count": 1, "append": False,
                })

    def test_adc_refresh_and_process_transfer_keep_credentials_refreshable(self):
        from types import SimpleNamespace
        credentials = SimpleNamespace(valid=False, token="first")

        def refresh(req):
            credentials.token = "refreshed"
            credentials.valid = True

        credentials.refresh = refresh
        with service() as server, patch.dict("os.environ", {}, clear=True), \
                patch("google.auth.default", return_value=(credentials, None)) as factory:
            store = create_store(server.connection)
            self.assertEqual(store.token, "refreshed")
            self.assertIsNone(snapshot_store(store)["token"])
            self.assertEqual(factory.call_count, 1)

    def test_explicit_login_and_per_run_settings_keep_google_credentials(self):
        import backprop
        with service() as server, tempfile.TemporaryDirectory() as directory:
            connection = Path(directory) / "connection.json"
            connection.write_text(json.dumps(server.connection))
            environment = {"BACKPROP_CONFIG": str(connection), "BACKPROP_TOKEN": "foundry-only"}
            with patch.dict("os.environ", environment, clear=True):
                try:
                    self.assertTrue(backprop.login(key=TOKEN, prompt=False))
                    self.assertEqual(os.environ["BACKPROP_GCP_ACCESS_TOKEN"], TOKEN)
                    run = backprop.init(project="google-login", dir=directory,
                        settings={"console": "off", "disable_git": True, "x_disable_stats": True})
                    run.log({"loss": 1})
                    run.finish()
                    backprop.teardown()
                    os.environ.pop("BACKPROP_GCP_ACCESS_TOKEN")
                    run = backprop.init(project="google-settings", dir=directory,
                        settings={"api_key": TOKEN, "console": "off", "disable_git": True, "x_disable_stats": True})
                    run.log({"loss": 2})
                    run.finish()
                finally:
                    backprop.teardown()


if __name__ == "__main__":
    unittest.main()
