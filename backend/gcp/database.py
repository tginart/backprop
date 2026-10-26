"""
Purpose: Persist Backprop event transactions in Firestore and artifact bytes in GCS.
Input: An explicit Firestore database, bucket, and installation prefix.
Output: Durable records, partition cursors, history snapshots, and immutable claims.
Note: A metrics batch reads its event identities and one partition cursor only.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import heapq
import io
import itertools
import json
import re
import tempfile
import time
from typing import Any, Iterator
import uuid

from google.api_core.exceptions import Aborted, AlreadyExists, NotFound, PreconditionFailed, ServiceUnavailable
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from backprop_backend.records import RecordConflict, canonical_record
from .queries import FIELDS, HistoryQuery, matches, parse_query, scope_key, scope_keys, sort_key


PARTITION_COUNT = 16
QUERY_RETENTION_S = 3600
QUERY_PAGE_SIZE = 128


class FirestoreDatabase:
    """
    Purpose: Implement the shared storage contract with native Firestore operations.
    Input: A configured Firestore client, private bucket, and installation prefix.
    Output: Atomic batches and claims without a shared database file or global counter.
    """

    def __init__(self, client: Any, bucket: Any, *, prefix: str = "backprop") -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_-]{0,200}", prefix):
            raise ValueError("Use a nonempty GCS prefix with letters, digits, /, _ or -")
        self.client, self.bucket, self.prefix = client, bucket, prefix.rstrip("/")
        namespace = hashlib.sha256((bucket.name + "/" + self.prefix).encode()).hexdigest()
        self.root = client.collection("installations").document(namespace)
        try:
            self.root.create({"gcp_id": uuid.uuid4().hex, "schema_version": 1,
                              "partition_count": PARTITION_COUNT})
        except AlreadyExists:
            pass
        descriptor = self.root.get().to_dict()
        if descriptor.get("schema_version") != 1 or descriptor.get("partition_count") != PARTITION_COUNT:
            raise ValueError("This Firestore installation requires a compatible backend version")
        self.installation_id = descriptor["gcp_id"]
        self.events = self.root.collection("events")

    def publish(self, records: list[dict]) -> None:
        """
        Purpose: Commit a whole batch with stable IDs and one partition cursor.
        Input: At most 500 canonical event records.
        Output: All new records become durable together; identical retries have no effect.
        Note: Firestore retries transactions that lose a concurrent identity or cursor race.
        """
        if len(records) > 500:
            raise ValueError("A Backprop batch exceeds 500 records")
        prepared = {}
        for record in records:
            record, canonical = canonical_record(record)
            identifier = _digest(record["event_id"])
            fingerprint = _digest(canonical)
            if identifier in prepared and prepared[identifier][1] != fingerprint:
                raise RecordConflict("Conflicting content for an event ID within the batch")
            prepared[identifier] = (record, fingerprint, canonical)
        if not prepared:
            return

        def commit(transaction):
            references = [self.events.document(identifier) for identifier in prepared]
            existing = {snapshot.id: snapshot.to_dict() for snapshot in self.client.get_all(
                references, field_paths=["fingerprint"], transaction=transaction,
            ) if snapshot.exists}
            for identifier, (_, fingerprint, _) in prepared.items():
                if identifier in existing and existing[identifier]["fingerprint"] != fingerprint:
                    raise RecordConflict("Conflicting content for an existing event ID")
            pending = [identifier for identifier in prepared if identifier not in existing]
            if not pending:
                return
            partition = int(pending[0][:8], 16) % PARTITION_COUNT
            cursor = self.root.collection("partitions").document(str(partition))
            previous = cursor.get(transaction=transaction).to_dict() or {}
            offset = previous.get("end", 0)
            for identifier in pending:
                record, fingerprint, canonical = prepared[identifier]
                transaction.create(self.events.document(identifier), {
                    "record": canonical, "fingerprint": fingerprint,
                    "partition": partition, "offset": offset,
                    "scope_keys": scope_keys(record),
                    "sort_key": sort_key(record["timestamp"], record["event_id"]),
                })
                offset += 1
            transaction.set(cursor, {"end": offset})

        self._transaction(commit)

    def end_offsets(self, *, read_time=None) -> dict[str, str]:
        read_time = read_time or self._read_time()
        references = [self.root.collection("partitions").document(str(index))
                      for index in range(PARTITION_COUNT)]
        result = {str(index): "0" for index in range(PARTITION_COUNT)}
        for snapshot in self.client.get_all(references, read_time=read_time):
            if snapshot.exists:
                result[snapshot.id] = str(snapshot.to_dict()["end"])
        return result

    def end_offset(self) -> int:
        return sum(map(int, self.end_offsets().values()))

    def read_records(self, *, start: int, limit: int, partition: str = "0") -> list[dict]:
        if partition not in {str(index) for index in range(PARTITION_COUNT)}:
            raise ValueError("Unknown Backprop Firestore partition")
        query = self.events.where(filter=FieldFilter("partition", "==", int(partition)))
        query = query.where(filter=FieldFilter("offset", ">=", start))
        return [{"offset": str(item["offset"]), "value": json.loads(item["record"])}
                for snapshot in query.order_by("offset").limit(limit).stream()
                for item in [snapshot.to_dict()]]

    def execute_query(self, query: str) -> str:
        """
        Purpose: Materialize one immutable CSV result at a fixed Firestore read time.
        Input: A validated SDK or dashboard history query.
        Output: A durable result ID with the exact partition boundaries at that read time.
        Note: The cache expires after one hour and survives service restarts.
        """
        plan = parse_query(query)
        read_time = self._read_time()
        boundaries = self.end_offsets(read_time=read_time)
        identifier = uuid.uuid4().hex
        name = f"{self.prefix}/queries/{identifier}.csv"
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as output:
            text = io.TextIOWrapper(output, encoding="utf-8", newline="", write_through=True)
            writer = csv.writer(text)
            writer.writerow(FIELDS.split(","))
            for record in itertools.islice(self._history_records(plan, read_time), plan.limit):
                writer.writerow([record[field] for field in FIELDS.split(",")])
            text.flush()
            output.seek(0)
            self.bucket.blob(name).upload_from_file(output, if_generation_match=0,
                                                   checksum="crc32c", timeout=180)
            text.detach()
        self.root.collection("queries").document(identifier).create({
            "blob": name, "boundaries": boundaries, "created_at": time.time(),
            "expires_at": datetime.fromtimestamp(time.time() + QUERY_RETENTION_S, timezone.utc),
        })
        return identifier

    def query_blob(self, identifier: str) -> tuple[Any, dict[str, str]]:
        descriptor = self.root.collection("queries").document(identifier).get().to_dict()
        if not descriptor or descriptor["created_at"] <= time.time() - QUERY_RETENTION_S:
            raise FileNotFoundError("Unknown or expired Backprop history query")
        return self.bucket.blob(descriptor["blob"]), descriptor["boundaries"]

    def query_page(self, identifier: str) -> tuple[bytes, dict[str, str]]:
        blob, boundaries = self.query_blob(identifier)
        return blob.download_as_bytes(timeout=180), boundaries

    def query_result(self, identifier: str) -> bytes:
        return self.query_page(identifier)[0]

    def has_query(self, identifier: str) -> bool:
        try:
            self.query_blob(identifier)
            return True
        except FileNotFoundError:
            return False

    def cancel_query(self, identifier: str) -> None:
        reference = self.root.collection("queries").document(identifier)
        descriptor = reference.get().to_dict()
        reference.delete()
        if descriptor:
            try:
                self.bucket.blob(descriptor["blob"]).delete()
            except NotFound:
                pass

    def upload_file(self, path: str, data: bytes, *, append: bool) -> None:
        """
        Purpose: Make artifact bytes durable before a transactional file reference.
        Input: A content-addressed file or an atomic APPEND resolution claim.
        Output: A committed reference or the existing immutable-identity conflict.
        """
        digest = hashlib.sha256(data).hexdigest()
        blob_path = re.fullmatch(r"blobs/([0-9a-f]{64})/[^/\\\x00-\x1f]+", path)
        claim = re.fullmatch(r"artifact-resolutions/[0-9a-f]{64}\.json", path)
        if blob_path:
            if append or blob_path[1] != digest:
                raise ValueError("Artifact bytes do not match their immutable address")
        elif claim and append:
            if not isinstance(json.loads(data), dict):
                raise ValueError("Artifact resolution must be a JSON object")
        else:
            raise ValueError("Unsupported Backprop file path or transaction type")
        digest_blob = self.bucket.blob(f"{self.prefix}/artifacts/{digest}")
        _create_blob(digest_blob, data)
        reference = self.root.collection("files").document(_digest(path))

        def commit(transaction):
            previous = reference.get(transaction=transaction).to_dict()
            if previous:
                if append:
                    raise FileExistsError("Artifact resolution already exists")
                if previous != {"path": path, "sha256": digest, "size": len(data)}:
                    raise RecordConflict("Conflicting content at an immutable file path")
                return
            transaction.create(reference, {"path": path, "sha256": digest, "size": len(data)})

        self._transaction(commit)

    def read_file(self, path: str) -> bytes:
        descriptor = self.root.collection("files").document(_digest(path)).get().to_dict()
        if descriptor is None:
            raise FileNotFoundError("File not found on the Backprop branch")
        try:
            data = self.bucket.blob(f"{self.prefix}/artifacts/{descriptor['sha256']}").download_as_bytes(timeout=60)
        except NotFound:
            raise OSError("The committed GCS artifact is absent") from None
        if len(data) != descriptor["size"] or hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
            raise OSError("Stored GCS artifact failed its integrity check")
        return data

    def upload_part(self, digest: str, index: int, data: bytes) -> None:
        _create_blob(self.bucket.blob(f"{self.prefix}/uploads/{digest}/{index}"), data)

    def complete_upload(self, digest: str, descriptor: dict) -> None:
        """
        Purpose: Validate staged bytes before a normal immutable file commit.
        Input: A SHA-256 identity, byte count, part count, and file address.
        Output: The ordinary durable file acknowledgement or atomic claim conflict.
        """
        if (set(descriptor) != {"path", "size_bytes", "part_count", "append"}
                or type(descriptor["size_bytes"]) is not int
                or not 0 < descriptor["size_bytes"] <= 512 * 1024 * 1024
                or type(descriptor["part_count"]) is not int
                or not 1 <= descriptor["part_count"] <= 64
                or type(descriptor["append"]) is not bool):
            raise ValueError("Invalid GCP upload descriptor")
        output = io.BytesIO()
        for index in range(descriptor["part_count"]):
            try:
                data = self.bucket.blob(f"{self.prefix}/uploads/{digest}/{index}").download_as_bytes(timeout=60)
            except NotFound:
                raise ValueError("The GCP upload is incomplete") from None
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("The GCP upload part exceeds its byte limit")
            output.write(data)
        data = output.getvalue()
        if len(data) != descriptor["size_bytes"] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("The GCP upload failed its integrity check")
        self.upload_file(descriptor["path"], data, append=descriptor["append"])

    def _transaction(self, operation) -> None:
        """
        Purpose: Keep exhausted Firestore contention retries retryable by SDK uploaders.
        Input: One native transaction operation.
        Output: A committed transaction or a transient Google service error.
        """
        try:
            firestore.transactional(operation)(self.client.transaction(max_attempts=20))
        except ValueError as error:
            if isinstance(error.__cause__, Aborted):
                raise ServiceUnavailable("Firestore transaction contention exceeded its retry budget") from None
            raise

    def _read_time(self):
        snapshot = self.root.get()
        if not snapshot.exists or snapshot.to_dict().get("gcp_id") != self.installation_id:
            raise OSError("The Firestore installation identity changed")
        return snapshot.read_time

    def _history_records(self, plan: HistoryQuery, read_time) -> Iterator[dict]:
        filters = dict(plan.filters)
        if any(filters[field] != value for field, value in plan.filters):
            return
        streams = [self._partition_history(index, plan, read_time)
                   for index in range(PARTITION_COUNT)]
        yield from heapq.merge(*streams, key=lambda row: (row["timestamp"], row["event_id"]),
                               reverse=plan.descending)

    def _partition_history(self, partition: int, plan: HistoryQuery, read_time) -> Iterator[dict]:
        """
        Purpose: Page one native index and resolve complete sort-prefix tie groups.
        Input: One partition, a query plan, and its immutable Firestore read time.
        Output: Exact record order, including identities longer than index field limits.
        Note: Only prefix ties require more than a bounded page before the next result.
        """
        query = self.events.where(filter=FieldFilter("partition", "==", partition))
        query = query.where(filter=FieldFilter("scope_keys", "array_contains",
                                              scope_key(tuple(dict(plan.filters).items()))))
        if plan.cursors:
            query = query.where(filter=FieldFilter("sort_key", "<=", sort_key(*min(plan.cursors))))
        direction = firestore.Query.DESCENDING if plan.descending else firestore.Query.ASCENDING
        page_size = min(QUERY_PAGE_SIZE, plan.limit + 1)
        query = query.order_by("sort_key", direction=direction).limit(page_size)
        pending, prefix, cursor = [], None, None
        while True:
            page = list((query.start_after(cursor) if cursor else query).stream(read_time=read_time))
            for snapshot in page:
                item = snapshot.to_dict()
                if prefix is not None and item["sort_key"] != prefix:
                    yield from sorted(pending, key=lambda row: (row["timestamp"], row["event_id"]),
                                      reverse=plan.descending)
                    pending = []
                prefix = item["sort_key"]
                record = json.loads(item["record"])
                if matches(record, plan):
                    pending.append(record)
            if len(page) < page_size:
                yield from sorted(pending, key=lambda row: (row["timestamp"], row["event_id"]),
                                  reverse=plan.descending)
                return
            cursor = page[-1]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _create_blob(blob: Any, data: bytes) -> None:
    try:
        blob.upload_from_string(data, if_generation_match=0, checksum="crc32c", timeout=60)
    except PreconditionFailed:
        if blob.download_as_bytes(timeout=60) != data:
            raise OSError("Stored GCS bytes failed their immutable identity check") from None
