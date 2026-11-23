"""
Purpose: Connect the SDK directly to a bucket installation, without a server.
Input: A bucket connection dictionary. Credentials come from ADC or boto3.
Output: The shared SDK store methods for events, files, artifacts and stops.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import threading

from backprop_backend.http_store import HttpStore
from backprop.control_store import matches
from .cache import ObjectCache
from .config import cache_directory, normalize_bucket_config
from .objects import BucketError, open_object_store
from .segments import (INSTALLATION_KEY, control_prefix, decode_segment,
                       ensure_installation, utc_now, write_records)


MAX_EVENT_BYTES = 900_000
CONTROL_TIMEOUT_S = 5
BLOB_PATH = re.compile(r"blobs/[0-9a-f]{64}/[^/\\]+")


class BucketStore:
    """
    Purpose: Write and read one bucket installation from a training process.
    Input: A bucket connection. The token argument exists for compatibility only.
    Output: Immutable segment writes and mirror-backed scoped reads.
    """

    backend_label = "Bucket Backprop"
    error_type = BucketError
    # Bucket objects cost one request each, so busy runs use larger batches.
    publish_batch_limits = (5000, 8_000_000)
    token = None
    event_to_record = staticmethod(HttpStore.event_to_record)
    record_to_event = staticmethod(HttpStore.record_to_event)

    def __init__(self, config: dict, token: str | None = None) -> None:
        """
        Purpose: Read the installation identity before a journal cursor is used.
        Input: A bucket connection with an optional pinned bucket_id.
        Output: A store with an installation-specific acknowledgement namespace.
        """
        self.config = normalize_bucket_config(config)
        self.uri = self.config["uri"]
        self._objects = open_object_store(self.uri)
        identifier = ensure_installation(self._objects)["bucket_id"]
        if self.config.get("bucket_id", identifier) != identifier:
            raise ValueError("Bucket installation changed; use its current connection")
        self.config["bucket_id"] = identifier
        self.ack_namespace = "bucket-" + identifier
        self._cache = ObjectCache(self._objects,
                                  cache_directory(self.uri, self.config.get("cache_dir")))
        self._mirror = None
        self._lock = threading.Lock()

    def publish(self, events: list[dict]) -> None:
        """
        Purpose: Write SDK events as immutable segments.
        Input: SDK event envelopes with stable event IDs.
        Output: Durable segments. A retry of the same events has no visible effect.
        """
        records = []
        for event in events:
            record = self.event_to_record(event)
            if len(json.dumps(record, ensure_ascii=True).encode()) > MAX_EVENT_BYTES:
                raise ValueError("Logging event exceeds 900 KB; split the table or upload a file")
            records.append(record)
        if not records:
            return
        written = write_records(self._objects, records, now=utc_now())
        with self._lock:
            mirror = self._mirror
        if mirror is not None:
            mirror.ingest_written(written)

    def read_events(self, project: str | None = None, run_id: str | None = None,
                    entity: str | None = None) -> list[dict]:
        """
        Purpose: Read scoped history from the bucket.
        Input: Optional project, run and entity filters.
        Output: Ordered SDK events without dashboard workspace snapshots.
        """
        from backprop.event_order import ordered_events
        events = [self.record_to_event(record) for record in
                  self.query_archive(project, run_id, entity)
                  if record["event_type"] != "workspace_snapshot"]
        return ordered_events(events)

    def query_archive(self, project: str | None = None, run_id: str | None = None,
                      entity: str | None = None) -> list[dict]:
        """
        Purpose: Return scoped stream records after an incremental refresh.
        Input: Optional project, run and entity filters.
        Output: Records by timestamp and event_id.
        Note: A run filter without a project cannot narrow the listing.
        """
        mirror = self._ensure_mirror()
        mirror.refresh(project=project, run_id=run_id if project is not None else None)
        return mirror.select_records(project=project, run_id=run_id, entity=entity)

    def control_reader(self, **scope):
        """Return a reader for stop requests addressed to one process instance."""
        return BucketControlReader(self._objects, **scope)

    def verify_credentials(self) -> bool:
        """Verify read and list access without a write."""
        self._objects.get(INSTALLATION_KEY)
        self._objects.list("events/", max_results=1)
        return True

    def upload_file(self, path: str | Path, name: str | None = None) -> dict:
        """
        Purpose: Store an immutable blob and return its stable file reference.
        Input: A local file and an optional display name.
        Output: The same reference fields as HttpStore.upload_file.
        """
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        filename = Path(name or Path(path).name).name
        file_path = f"blobs/{digest}/{filename}"
        self._objects.put_content_addressed("files/" + file_path, data=data)
        return {"dataset_rid": self.config["media_dataset_rid"], "path": file_path,
                "branch": self.config["branch"],
                "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "size": len(data), "sha256": digest,
                "digest": base64.b64encode(hashlib.md5(data).digest()).decode()}

    def file_url(self, reference: dict) -> str:
        return f"{self.uri}/files/{self._file_path(reference)}"

    def download_file(self, reference: dict) -> bytes:
        """
        Purpose: Read exact file bytes and verify them before a caller uses them.
        Input: A file reference from upload_file.
        Output: Bytes that match the reference SHA-256 and size.
        """
        path = self._file_path(reference)
        expected = reference.get("sha256") or path.split("/")[1]
        try:
            data = self._cache.read_blob("files/" + path, expected)
        except OSError as error:
            raise self.error_type(str(error)) from error
        if reference.get("size") is not None and len(data) != reference["size"]:
            raise self.error_type("Downloaded file size does not match its manifest")
        return data

    def read_artifact_resolution(self, key: str) -> dict | None:
        """Return a saved incremental-artifact choice, or None if it is absent."""
        try:
            data = self._objects.get(_resolution_key(key))
        except FileNotFoundError:
            return None
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Artifact resolution file must contain a JSON object")
        return value

    def claim_artifact_resolution(self, key: str, candidate: dict) -> dict:
        """
        Purpose: Keep the first completed choice for one artifact identity.
        Input: The SHA-256 identity and this writer's candidate.
        Output: The winning choice, which can be an earlier writer's.
        """
        data = json.dumps(candidate, allow_nan=False, separators=(",", ":")).encode()
        if self._objects.put_create_only(_resolution_key(key), data=data):
            return json.loads(data)
        winner = self.read_artifact_resolution(key)
        if winner is None:
            raise self.error_type("Committed artifact resolution is not yet readable; retry",
                                  retryable=True)
        return winner

    def _ensure_mirror(self):
        from .mirror import BucketMirror
        with self._lock:
            if self._mirror is None:
                self._mirror = BucketMirror(self._objects, self._cache,
                                            bucket_id=self.config["bucket_id"])
            return self._mirror

    def _file_path(self, reference: dict) -> str:
        if reference.get("dataset_rid") != self.config["media_dataset_rid"]:
            raise ValueError("File reference belongs to a different backend dataset")
        path = reference.get("path", "")
        if not isinstance(path, str) or not BLOB_PATH.fullmatch(path):
            raise ValueError("Invalid content-addressed Backprop file reference")
        if reference.get("sha256") is not None and reference["sha256"] != path.split("/")[1]:
            raise ValueError("File content address disagrees with its SHA-256 manifest")
        if reference.get("branch", self.config["branch"]) != self.config["branch"]:
            raise ValueError("File reference belongs to a different backend branch")
        return path


class BucketControlReader:
    """
    Purpose: Follow stop requests for one process instance.
    Input: An object store and the project, entity, run_id and instance_id scope.
    Output: Matching stop request events, each one time.
    Note: Each instance has its own control prefix, so a full listing is small
        and needs no start key. Writer clocks therefore have no effect.
    """

    def __init__(self, objects, **scope) -> None:
        self._objects = objects
        self.scope = scope
        self._prefix = control_prefix(scope["project"], scope["run_id"], scope["instance_id"])
        self._seen: set[str] = set()

    def poll(self) -> list[dict]:
        keys, _ = self._objects.list(self._prefix, timeout_s=CONTROL_TIMEOUT_S)
        events = []
        for key in sorted(key for key in keys if key not in self._seen):
            records = decode_segment(self._objects.get(key, timeout_s=CONTROL_TIMEOUT_S))
            for record in records:
                event = HttpStore.record_to_event(record)
                if matches(event, **self.scope):
                    events.append(event)
            self._seen.add(key)
        return events


def _resolution_key(key: str) -> str:
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise ValueError("Artifact resolution key must be a SHA-256 identity")
    return f"files/artifact-resolutions/{key}.json"
