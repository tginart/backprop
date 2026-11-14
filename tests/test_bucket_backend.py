"""
Purpose: Exercise the bucket backend through its object, mirror, SDK and HTTP boundaries.
Input: Temporary file:// buckets, an in-memory GCS fake and loopback servers.
Output: Assertions for create-only writes, dedupe, stable offsets and SDK use.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import request
import uuid

try:
    import duckdb  # noqa: F401
except ImportError as error:
    raise unittest.SkipTest("Bucket backend tests require duckdb") from error

import backprop
from backprop.failures import retryable_failure
from backprop_backend.bucket.cache import ObjectCache
from backprop_backend.bucket.config import cache_directory, normalize_bucket_config
from backprop_backend.bucket.mirror import SWEEP_INTERVAL_S, BucketMirror
from backprop_backend.bucket.objects import (BucketError, FileObjects, GcsObjects,
                                             open_object_store)
from backprop_backend.bucket.segments import (control_prefix, decode_segment,
                                              ensure_installation, write_records)
from backprop_backend.bucket.server import running_bucket_server
from backprop_backend.bucket.store import BucketStore
from backprop_backend.connection import (create_store, current_init_session,
                                         is_backend_store, load_config, restore_store,
                                         snapshot_store)
from backprop_backend.records import RecordConflict


STREAM = "/api/v2/highScale/streams/datasets/ri.foundry.local.dataset.events/streams/master"
EPOCH_S = 1_800_000_000.0


def record(identifier: str, *, run_id: str = "run-1", step: int = 0,
           event_type: str = "history", payload: dict | None = None) -> dict:
    return {"schema_version": 1, "event_id": identifier, "run_id": run_id,
            "project": "project", "entity": "team", "event_type": event_type,
            "timestamp": f"2026-01-01T00:00:{step:02d}+00:00",
            "payload": json.dumps(payload or {"step": step, "metrics": {"loss": step}})}


def event(identifier: str, **options) -> dict:
    value = record(identifier, **options)
    value["type"] = value.pop("event_type")
    value["payload"] = json.loads(value["payload"])
    return value


def moment(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


class BucketTestCase(unittest.TestCase):
    """
    Purpose: Give each test a private file:// bucket and object cache.
    Input: A temporary directory.
    Output: self.uri, self.objects and an isolated BACKPROP_BUCKET_CACHE_DIR.
    """

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.uri = "file://" + str(self.root / "bucket")
        self.objects = open_object_store(self.uri)
        self.stack.enter_context(patch.dict(os.environ, {
            "BACKPROP_BUCKET_CACHE_DIR": str(self.root / "cache")}))

    def mirror(self, *, name: str = "viewer", clock=None, persistent: bool = False) -> BucketMirror:
        bucket_id = ensure_installation(self.objects)["bucket_id"]
        cache = ObjectCache(self.objects, self.root / name)
        path = self.root / name / "mirror.duckdb" if persistent else None
        options = {"clock": clock} if clock else {}
        mirror = BucketMirror(self.objects, cache, bucket_id=bucket_id, database_path=path,
                              **options)
        self.addCleanup(mirror.close)
        return mirror


class ObjectStoreContract:
    """Shared create-only and listing assertions for every tested adapter."""

    def test_create_only_keeps_the_first_bytes(self) -> None:
        self.assertTrue(self.store.put_create_only("a/b.json", data=b"first"))
        self.assertFalse(self.store.put_create_only("a/b.json", data=b"second"))
        self.assertEqual(self.store.get("a/b.json"), b"first")
        with self.assertRaises(FileNotFoundError):
            self.store.get("a/missing.json")

    def test_concurrent_creates_have_one_winner(self) -> None:
        with ThreadPoolExecutor(8) as workers:
            created = list(workers.map(
                lambda index: self.store.put_create_only("claim", data=str(index).encode()),
                range(16)))
        self.assertEqual(created.count(True), 1)

    def test_listing_supports_delimiters_and_start_keys(self) -> None:
        for key in ("events/p=1/r=1/001", "events/p=1/r=1/002", "events/p=1/r=2/001",
                    "events/p=2/r=1/001", "other/x"):
            self.store.put_create_only(key, data=b"x")
        self.assertEqual(self.store.list("events/", delimiter="/")[1],
                         ["events/p=1/", "events/p=2/"])
        self.assertEqual(self.store.list("events/p=1/", delimiter="/")[1],
                         ["events/p=1/r=1/", "events/p=1/r=2/"])
        self.assertEqual(self.store.list("events/p=1/r=1/", start_offset="events/p=1/r=1/002")[0],
                         ["events/p=1/r=1/002"])
        self.assertEqual(len(self.store.list("events/", max_results=1)[0]), 1)


class FileObjectsTests(ObjectStoreContract, unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = FileObjects(Path(directory.name))


class GcsObjectsTests(ObjectStoreContract, unittest.TestCase):
    """
    Purpose: Check the GCS adapter against an in-memory client.
    Input: A fake client with generation preconditions and prefix listings.
    Output: The same contract as the file adapter, plus error classification.
    """

    def setUp(self) -> None:
        try:
            from google.api_core import exceptions
        except ImportError as error:
            raise unittest.SkipTest("GCS adapter tests require google-cloud-storage") from error
        self.exceptions = exceptions
        self.client = FakeGcsClient()
        self.store = GcsObjects("fixture", "root/prefix", client=self.client)

    def test_keys_are_relative_to_the_installation_prefix(self) -> None:
        self.store.put_create_only("events/a", data=b"x")
        self.assertEqual(list(self.client.objects), ["root/prefix/events/a"])

    def test_provider_errors_carry_a_retry_classification(self) -> None:
        import requests
        cases = [(self.exceptions.ServiceUnavailable("busy"), True),
                 (self.exceptions.TooManyRequests("slow down"), True),
                 (self.exceptions.Forbidden("denied"), False),
                 (requests.exceptions.ConnectionError("reset"), True)]
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                self.client.failure = failure
                with self.assertRaises(BucketError) as caught:
                    self.store.put_create_only("events/a", data=b"x")
                self.assertIs(caught.exception.retryable, expected)
                self.assertIs(retryable_failure(caught.exception), expected)


class SegmentTests(BucketTestCase):
    def test_a_retried_batch_writes_the_same_key(self) -> None:
        now = moment(EPOCH_S)
        first = write_records(self.objects, [record("a"), record("b", step=1)], now=now)
        second = write_records(self.objects, [record("a"), record("b", step=1)], now=now)
        self.assertEqual([key for key, _ in first], [key for key, _ in second])
        self.assertEqual(decode_segment(first[0][1])[1]["event_id"], "b")

    def test_stop_requests_are_grouped_by_process_instance(self) -> None:
        stop = record("s", event_type="run_stop_request",
                      payload={"action": "stop", "instance_id": "abc"})
        keys = [key for key, _ in write_records(self.objects, [record("a"), stop],
                                                now=moment(EPOCH_S))]
        self.assertTrue(keys[0].startswith("events/project=project/run_id=run-1/"))
        self.assertTrue(keys[1].startswith(control_prefix("project", "run-1", "abc")))

    def test_an_invalid_record_prevents_every_write(self) -> None:
        invalid = {**record("b"), "payload": "[]"}
        with self.assertRaises(ValueError):
            write_records(self.objects, [record("a"), invalid], now=moment(EPOCH_S))
        self.assertEqual(self.objects.list("events/")[0], [])


class MirrorTests(BucketTestCase):
    def test_duplicates_and_conflicts_keep_the_first_record(self) -> None:
        mirror = self.mirror()
        write_records(self.objects, [record("a"), record("b", step=1)], now=moment(EPOCH_S))
        self.assertEqual(mirror.refresh(), 2)
        write_records(self.objects, [record("a"), record("b", step=1)], now=moment(EPOCH_S + 1))
        write_records(self.objects, [record("a", payload={"changed": True})],
                      now=moment(EPOCH_S + 2))
        self.assertEqual(mirror.refresh(), 0)
        self.assertEqual(mirror.conflict_count(), 1)
        rows = mirror.select_records()
        self.assertEqual(json.loads(rows[0]["payload"]), {"metrics": {"loss": 0}, "step": 0})

    def test_publish_rejects_a_conflict_before_a_write(self) -> None:
        mirror = self.mirror()
        mirror.publish([record("a")])
        mirror.publish([record("a")])
        keys = self.objects.list("events/")[0]
        with self.assertRaises(RecordConflict):
            mirror.publish([record("a", payload={"changed": True})])
        self.assertEqual(self.objects.list("events/")[0], keys)
        self.assertEqual(mirror.end_offset(), 1)

    def test_offsets_stay_gap_free_while_writers_and_refreshes_overlap(self) -> None:
        mirror = self.mirror()
        writers = [BucketStore({"backend": "bucket", "uri": self.uri}) for _ in range(3)]
        observed, done = [], threading.Event()

        def follow() -> None:
            cursor = 0
            while not done.is_set() or cursor < mirror.end_offset():
                rows = mirror.read_records(start=cursor, limit=7)
                for row in rows:
                    observed.append(int(row["offset"]))
                    cursor = int(row["offset"]) + 1

        def write(index: int) -> None:
            for batch in range(10):
                identifier = f"w{index}-{batch}"
                if index == 0:
                    mirror.publish([record(identifier + "-viewer", step=batch)])
                writers[index].publish([event(identifier, step=batch)])
                mirror.refresh()

        reader = threading.Thread(target=follow)
        reader.start()
        with ThreadPoolExecutor(3) as workers:
            list(workers.map(write, range(3)))
        mirror.refresh()
        done.set()
        reader.join()
        self.assertEqual(mirror.end_offset(), 40)
        self.assertEqual(observed, list(range(40)))

    def test_lookback_and_full_sweep_find_late_segments(self) -> None:
        now = [EPOCH_S]
        mirror = self.mirror(clock=lambda: now[0])
        write_records(self.objects, [record("a")], now=moment(now[0]))
        self.assertEqual(mirror.refresh(), 1)
        now[0] += 10
        write_records(self.objects, [record("b")], now=moment(now[0] - 120))
        self.assertEqual(mirror.refresh(), 1)
        now[0] += 10
        write_records(self.objects, [record("c")], now=moment(now[0] - 3600))
        self.assertEqual(mirror.refresh(), 0)
        now[0] += SWEEP_INTERVAL_S
        self.assertEqual(mirror.refresh(), 1)

    def test_persistent_mirror_resets_for_a_new_installation(self) -> None:
        mirror = self.mirror(persistent=True)
        mirror.publish([record("a")])
        viewer = mirror.local_id
        mirror.close()
        reopened = self.mirror(persistent=True)
        self.assertEqual((reopened.end_offset(), reopened.local_id), (1, viewer))
        self.assertNotEqual(self.mirror(name="other").local_id, viewer)
        reopened.close()
        cache = ObjectCache(self.objects, self.root / "viewer")
        moved = BucketMirror(self.objects, cache, bucket_id=uuid.uuid4().hex,
                             database_path=self.root / "viewer" / "mirror.duckdb")
        self.addCleanup(moved.close)
        self.assertEqual(moved.end_offset(), 0)


class StoreTests(BucketTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = BucketStore({"backend": "bucket", "uri": self.uri})

    def test_read_events_is_scoped_ordered_and_sees_other_writers(self) -> None:
        other = BucketStore({"backend": "bucket", "uri": self.uri})
        self.store.publish([event("b", step=2), event("x", run_id="run-2")])
        self.assertEqual([e["event_id"] for e in self.store.read_events("project", "run-1")], ["b"])
        other.publish([event("a", step=1), event("w", event_type="workspace_snapshot")])
        events = self.store.read_events("project", "run-1")
        self.assertEqual([e["event_id"] for e in events], ["a", "b"])
        self.assertEqual(len(self.store.read_events()), 3)
        self.assertEqual(self.store.read_events(entity="nobody"), [])

    def test_files_are_content_addressed_and_verified(self) -> None:
        source = self.root / "weights.bin"
        source.write_bytes(b"weights")
        reference = self.store.upload_file(source, "ignored/model.bin")
        self.assertEqual(reference["path"], f"blobs/{hashlib.sha256(b'weights').hexdigest()}/model.bin")
        self.assertEqual(set(reference), {"dataset_rid", "path", "branch", "mime_type", "size",
                                          "sha256", "digest"})
        self.assertEqual(self.store.download_file(reference), b"weights")
        self.assertEqual(self.store.file_url(reference), f"{self.uri}/files/{reference['path']}")
        tampered = BucketStore({"backend": "bucket", "uri": self.uri,
                                "cache_dir": str(self.root / "fresh")})
        (self.root / "bucket" / "files" / reference["path"]).write_bytes(b"tampered")
        with self.assertRaises(BucketError):
            tampered.download_file(reference)

    def test_concurrent_artifact_claims_have_one_winner(self) -> None:
        key = hashlib.sha256(b"artifact").hexdigest()
        self.assertIsNone(self.store.read_artifact_resolution(key))
        with ThreadPoolExecutor(8) as workers:
            winners = list(workers.map(
                lambda index: self.store.claim_artifact_resolution(key, {"choice": index}),
                range(16)))
        self.assertEqual(len({json.dumps(winner) for winner in winners}), 1)
        self.assertEqual(self.store.read_artifact_resolution(key), winners[0])

    def test_control_reader_returns_only_its_own_new_requests(self) -> None:
        scope = {"project": "project", "entity": "team", "run_id": "run-1", "instance_id": "mine"}
        reader = self.store.control_reader(**scope)
        self.assertEqual(reader.poll(), [])
        for identifier, instance in (("s1", "mine"), ("s2", "other")):
            self.store.publish([event(identifier, event_type="run_stop_request",
                                      payload={"action": "stop", "instance_id": instance})])
        self.assertEqual([e["event_id"] for e in reader.poll()], ["s1"])
        self.assertEqual(reader.poll(), [])

    def test_connection_selects_and_snapshots_the_bucket_store(self) -> None:
        with patch.dict(os.environ, {"BACKPROP_BACKEND": "bucket", "BACKPROP_BUCKET_URI": self.uri,
                                     "BACKPROP_HOST": "https://ignored.example"}):
            os.environ.pop("BACKPROP_CONFIG", None)
            config = load_config()
        self.assertEqual(config["host"], config["uri"])
        store = create_store(config)
        self.assertTrue(is_backend_store(store))
        descriptor = pickle.loads(pickle.dumps(snapshot_store(store)))
        self.assertEqual(descriptor["kind"], "backend")
        self.assertEqual(restore_store(descriptor).ack_namespace, store.ack_namespace)
        self.assertIsNone(current_init_session(config["host"]))
        with self.assertRaises(ValueError):
            normalize_bucket_config({"backend": "bucket", "uri": "https://bucket/prefix"})
        with self.assertRaises(ValueError):
            BucketStore({**config, "bucket_id": uuid.uuid4().hex})
        self.assertEqual(cache_directory("gs://a-b/x"), cache_directory("gs://a-b/x/"))


class ServerTests(BucketTestCase):
    """
    Purpose: Verify the dashboard HTTP contract over a bucket mirror.
    Input: A running bucket server and data written by an SDK store.
    Output: Local-contract responses and write-through stop requests.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = BucketStore({"backend": "bucket", "uri": self.uri})
        self.store.publish([event("a", step=1), event("b", step=2)])
        self.server = self.stack.enter_context(running_bucket_server(
            self.uri, cache_dir=self.root / "viewer", refresh_s=0.2))
        self.origin = self.server.origin

    def call(self, path: str, body=None, *, content_type: str = "application/json",
             headers: dict | None = None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = request.Request(self.origin + path, data=data, method="POST" if data else "GET",
                              headers={"Content-Type": content_type, **(headers or {})})
        with request.urlopen(req, timeout=10) as response:
            return response.read(), response.headers

    def test_config_and_stream_reads_use_the_local_contract(self) -> None:
        config = json.loads(self.call("/config.json")[0])
        self.assertEqual(config["backend"], "local")
        self.assertEqual(config["local_id"], self.server.database.local_id)
        self.assertEqual(json.loads(self.call(STREAM + "/getEndOffsets?preview=true")[0]), {"0": "2"})
        rows = json.loads(self.call(STREAM + "/getRecords?preview=true&partitionId=0&startOffset=1&limit=10")[0])
        self.assertEqual([(row["offset"], row["value"]["event_id"]) for row in rows], [("1", "b")])

    def test_history_query_reports_its_boundary(self) -> None:
        query = "SELECT * FROM `ri.foundry.local.dataset.events` ORDER BY timestamp LIMIT 1000000"
        status = json.loads(self.call("/api/v2/sqlQueries/execute", {
            "query": query, "serializationFormat": "CSV", "fallbackBranchIds": ["master"]})[0])
        content, headers = self.call(f"/api/v2/sqlQueries/{status['queryId']}/getResults")
        self.assertEqual(headers["X-Backprop-End-Offset"], "2")
        self.assertEqual(len(content.decode().strip().splitlines()), 3)

    def test_new_bucket_events_appear_after_a_refresh(self) -> None:
        self.store.publish([event("c", step=3)])
        for _ in range(100):
            if self.server.database.end_offset() == 3:
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.server.database.end_offset(), 3)

    def test_stop_requests_and_files_are_written_through(self) -> None:
        stop = record("stop", event_type="run_stop_request",
                      payload={"action": "stop", "instance_id": "abc"})
        self.call(STREAM + "/publishRecords", {"records": [stop]})
        self.assertEqual(len(self.objects.list(control_prefix("project", "run-1", "abc"))[0]), 1)
        data = b"0123456789"
        path = f"blobs/{hashlib.sha256(data).hexdigest()}/digits.txt"
        files = "/api/v2/datasets/ri.foundry.local.dataset.media/files/"
        self.call(f"{files}{path}/upload?branchName=master", data,
                  content_type="application/octet-stream")
        content, headers = self.call(f"{files}{path}/content?branchName=master",
                                     headers={"Range": "bytes=2-4"})
        self.assertEqual((content, headers["Content-Range"]), (b"234", "bytes 2-4/10"))


class AcceptanceTests(BucketTestCase):
    def test_public_sdk_scenario_against_a_file_bucket(self) -> None:
        from backend_acceptance import run_scenario
        result = run_scenario({"backend": "bucket", "uri": self.uri}, token=None,
                              archive_timeout_s=5)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["checks"]), 11)


class FakeGcsClient:
    """
    Purpose: Model GCS create preconditions and lexicographic prefix listings.
    Input: Calls from GcsObjects.
    Output: Stored bytes, PreconditionFailed for existing objects, or failure.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.failure: Exception | None = None

    def bucket(self, name: str):
        return FakeGcsBucket(self)

    def list_blobs(self, bucket, *, prefix, delimiter=None, start_offset=None,
                   max_results=None, **options):
        return FakeGcsPages(sorted(self.objects), prefix, delimiter, start_offset, max_results)


class FakeGcsBucket:
    def __init__(self, client: FakeGcsClient) -> None:
        self.client = client

    def blob(self, name: str):
        return FakeGcsBlob(self.client, name)


class FakeGcsBlob:
    def __init__(self, client: FakeGcsClient, name: str) -> None:
        self.client, self.name = client, name

    def upload_from_string(self, data, *, if_generation_match, **options):
        from google.api_core.exceptions import PreconditionFailed
        assert if_generation_match == 0
        if self.client.failure is not None:
            raise self.client.failure
        with self.client.lock:
            if self.name in self.client.objects:
                raise PreconditionFailed("exists")
            self.client.objects[self.name] = bytes(data)

    def download_as_bytes(self, **options):
        from google.api_core.exceptions import NotFound
        with self.client.lock:
            if self.name not in self.client.objects:
                raise NotFound("absent")
            return self.client.objects[self.name]


class FakeGcsPages:
    """An iterator of blobs whose prefixes are known after iteration, as in GCS."""

    def __init__(self, names, prefix, delimiter, start_offset, max_results) -> None:
        self.prefixes, self._blobs = set(), []
        for name in names:
            if not name.startswith(prefix) or (start_offset and name < start_offset):
                continue
            rest = name[len(prefix):]
            if delimiter and delimiter in rest:
                self.prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            elif max_results is None or len(self._blobs) < max_results:
                self._blobs.append(type("Blob", (), {"name": name}))

    def __iter__(self):
        return iter(self._blobs)


if __name__ == "__main__":
    unittest.main()
