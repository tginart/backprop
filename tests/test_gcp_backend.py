"""
Purpose: Verify GCS commit semantics and the existing public acceptance scenario.
Input: A generation-aware bucket fixture and real HTTP service requests.
Output: Evidence for concurrent writers, restart, retries, Google isolation, and SDK parity.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib import request

try:
    from google.api_core.exceptions import NotFound, PreconditionFailed
except ImportError:
    raise unittest.SkipTest("Install the gcp extra for GCS contract coverage")

from backend_acceptance import run_scenario
from backprop_backend.connection import create_store, snapshot_store
from backprop_backend.gcp.auth import GoogleAuthorizer
from backprop_backend.gcp.config import STREAM_RID, normalize_gcp_config
from backprop_backend.gcp.database import GcsDatabase
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


@contextmanager
def service(bucket=None):
    """
    Purpose: Exercise real HTTP while the production origin remains HTTPS-only.
    Input: An optional persistent generation-aware bucket.
    Output: A GCP server and a test-only loopback transport.
    """
    database = GcsDatabase(bucket or Bucket())
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


class GcpDatabaseTests(unittest.TestCase):
    """Verify the durable shared storage contract across independent instances."""

    def setUp(self):
        self.bucket = Bucket()
        self.database = GcsDatabase(self.bucket)

    def test_concurrent_writers_restart_and_conflicting_batch_are_atomic(self):
        instances = [GcsDatabase(self.bucket) for _ in range(4)]
        records = [HttpStore.event_to_record(event(f"event-{i}")) for i in range(24)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda pair: pair[0].publish([pair[1]]),
                          [(instances[i % 4], row) for i, row in enumerate(records)]))
        restored = GcsDatabase(self.bucket)
        self.assertEqual(restored.local_id, self.database.local_id)
        self.assertEqual(restored.end_offset(), 24)
        restored.publish(records)
        self.assertEqual(restored.end_offset(), 24)
        with self.assertRaises(LocalConflict):
            restored.publish([HttpStore.event_to_record(event("new")),
                              {**records[0], "run_id": "conflict"}])
        self.assertEqual(restored.end_offset(), 24)
        self.assertEqual([row["offset"] for row in restored.read_records(start=0, limit=100)],
                         [str(i) for i in range(24)])

    def test_history_boundary_query_restart_cancel_and_expiry(self):
        self.database.publish([HttpStore.event_to_record(event("first"))])
        query = f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100"
        identifier = self.database.execute_query(query)
        self.database.publish([HttpStore.event_to_record(event("second"))])
        restored = GcsDatabase(self.bucket)
        result, boundary = restored.query_page(identifier)
        self.assertEqual(boundary, 1)
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

    def test_artifacts_integrity_and_concurrent_append_claims(self):
        data = b"immutable artifact"
        path = f"blobs/{hashlib.sha256(data).hexdigest()}/weights.bin"
        self.database.upload_file(path, data, append=False)
        self.assertEqual(GcsDatabase(self.bucket).read_file(path), data)
        with self.assertRaises(ValueError):
            self.database.upload_file(path, b"wrong", append=False)
        claim = "artifact-resolutions/" + "a" * 64 + ".json"

        def compete(i):
            try:
                GcsDatabase(self.bucket).upload_file(claim, json.dumps({"winner": i}).encode(), append=True)
                return i
            except FileExistsError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = [i for i in pool.map(compete, range(8)) if i is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(json.loads(self.database.read_file(claim)), {"winner": winners[0]})

    def test_prefixes_isolate_installations(self):
        isolated = GcsDatabase(self.bucket, prefix="other")
        self.assertNotEqual(isolated.local_id, self.database.local_id)
        self.database.publish([HttpStore.event_to_record(event("private"))])
        self.assertEqual(isolated.end_offset(), 0)

    def test_read_retries_a_generation_removed_between_reload_and_download(self):
        self.database.publish([HttpStore.event_to_record(event("durable"))])
        original = Blob.download_as_bytes
        attempts = []

        def race(blob, **options):
            if blob.name.endswith("state.sqlite3") and not attempts:
                attempts.append(1)
                raise NotFound("The observed generation was replaced")
            return original(blob, **options)

        with patch.object(Blob, "download_as_bytes", race):
            self.assertEqual(self.database.end_offset(), 1)

    def test_query_expiry_and_corrupt_artifacts_fail_explicitly(self):
        query = f"SELECT * FROM `{STREAM_RID}` ORDER BY timestamp LIMIT 100"
        with patch("time.time", return_value=10000):
            identifier = self.database.execute_query(query)
        with patch("time.time", return_value=13600):
            self.assertFalse(self.database.has_query(identifier))
        data = b"integrity"
        digest = hashlib.sha256(data).hexdigest()
        path = f"blobs/{digest}/weights"
        self.database.upload_file(path, data, append=False)
        name = self.database.prefix + "/artifacts/" + digest
        self.bucket.objects[name] = (self.bucket.objects[name][0], b"corrupt")
        with self.assertRaises(OSError):
            self.database.read_file(path)


class GcpHttpTests(unittest.TestCase):
    """Run SDK acceptance with the real HTTP contract and a GCS fixture."""

    def test_existing_shared_acceptance_scenario(self):
        with service() as server, patch.dict("os.environ", {"BACKPROP_GCP_ACCESS_TOKEN": TOKEN}):
            result = run_scenario(server.connection, token=None, archive_timeout_s=5)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(result["checks"]), 11)

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
                        records = server.database.read_records(start=0, limit=100)
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
