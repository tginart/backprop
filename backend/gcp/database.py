"""
Purpose: Persist atomic Backprop transactions and immutable artifacts in GCS.
Input: An operator-selected bucket and an isolated object prefix.
Output: Durable offsets, snapshots, files, and claims across Cloud Run instances.
Note: SQLite evaluates each private snapshot. GCS generation preconditions commit it.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import io
from pathlib import Path
import random
import re
import sqlite3
import tempfile
import time
from typing import Callable, Any

from google.api_core.exceptions import NotFound, PreconditionFailed

from backprop_backend.local.config import STREAM_RID as LOCAL_STREAM_RID
from backprop_backend.local.database import LocalDatabase
from .config import STREAM_RID


MAX_COMMIT_ATTEMPTS = 20


class GcsDatabase:
    """
    Purpose: Serialize complete database commits with GCS compare-and-swap.
    Input: A private bucket with strongly consistent generation preconditions.
    Output: The same database operations used by the local HTTP contract.
    Note: No shared filesystem, process lock, or Cloud Run disk is authoritative.
    """

    def __init__(self, bucket: Any, *, prefix: str = "backprop") -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_-]{0,200}", prefix):
            raise ValueError("Use a nonempty GCS prefix with letters, digits, /, _ or -")
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.local_id = self._transaction(lambda database: database.local_id, write=True)

    def publish(self, records: list[dict]) -> None:
        self._transaction(lambda database: database.publish(records), write=True)

    def end_offset(self) -> int:
        return self._transaction(lambda database: database.end_offset())

    def read_records(self, *, start: int, limit: int) -> list[dict]:
        return self._transaction(lambda database: database.read_records(start=start, limit=limit))

    def execute_query(self, query: str) -> str:
        prefix = f"SELECT * FROM `{STREAM_RID}`"
        if not query.startswith(prefix):
            raise ValueError("Only Backprop GCP history queries are supported")
        query = f"SELECT * FROM `{LOCAL_STREAM_RID}`" + query[len(prefix):]
        return self._transaction(lambda database: database.execute_query(query), write=True)

    def query_page(self, identifier: str) -> tuple[bytes, int]:
        return self._transaction(lambda database: database.query_page(identifier))

    def query_result(self, identifier: str) -> bytes:
        return self.query_page(identifier)[0]

    def has_query(self, identifier: str) -> bool:
        return self._transaction(lambda database: database.has_query(identifier))

    def cancel_query(self, identifier: str) -> None:
        self._transaction(lambda database: database.cancel_query(identifier), write=True)

    def upload_file(self, path: str, data: bytes, *, append: bool) -> None:
        """
        Purpose: Commit verified bytes before a reference or atomic claim.
        Input: An immutable blob address or APPEND claim and complete bytes.
        Output: A durable reference after the GCS snapshot commit succeeds.
        """
        digest = hashlib.sha256(data).hexdigest()

        def upload(database: LocalDatabase) -> None:
            database.upload_file(path, data, append=append)
            blob = self.bucket.blob(f"{self.prefix}/artifacts/{digest}")
            try:
                blob.upload_from_string(data, if_generation_match=0, checksum="crc32c", timeout=60)
            except PreconditionFailed:
                if blob.download_as_bytes(timeout=60) != data:
                    raise OSError("Stored GCS artifact failed its integrity check") from None

        self._transaction(upload, write=True)

    def read_file(self, path: str) -> bytes:
        """
        Purpose: Read only a committed file reference and verify its exact bytes.
        Input: An immutable Backprop file path.
        Output: Complete GCS bytes after size and SHA-256 checks.
        """
        def reference(database: LocalDatabase) -> tuple[str, int]:
            with database.connect() as connection:
                row = connection.execute("SELECT sha256,size FROM files WHERE path=?", (path,)).fetchone()
            if row is None:
                raise FileNotFoundError("File not found on the Backprop branch")
            return row[0], row[1]

        digest, size = self._transaction(reference)
        try:
            data = self.bucket.blob(f"{self.prefix}/artifacts/{digest}").download_as_bytes(timeout=60)
        except NotFound:
            raise OSError("The committed GCS artifact is absent") from None
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise OSError("Stored GCS artifact failed its integrity check")
        return data

    def upload_part(self, digest: str, index: int, data: bytes) -> None:
        blob = self.bucket.blob(f"{self.prefix}/uploads/{digest}/{index}")
        try:
            blob.upload_from_string(data, if_generation_match=0, checksum="crc32c", timeout=60)
        except PreconditionFailed:
            if blob.download_as_bytes(timeout=60) != data:
                raise ValueError("Conflicting immutable upload part") from None

    def complete_upload(self, digest: str, descriptor: dict) -> None:
        """
        Purpose: Validate complete staged bytes before an immutable file commit.
        Input: A SHA-256 identity, exact byte count, part count, and file address.
        Output: The ordinary durable file acknowledgement or an atomic claim conflict.
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

    def _transaction(self, operation: Callable, *, write: bool = False) -> Any:
        """
        Purpose: Retry a complete transaction when another instance commits first.
        Input: A deterministic database operation and its mutation policy.
        Output: A result only after a generation-conditional durable commit.
        Method:
            1. Download one exact object generation into a private temporary directory.
            2. Apply the existing SQLite operation and create a consistent backup.
            3. Replace only the downloaded generation or retry from the latest snapshot.
        """
        for attempt in range(MAX_COMMIT_ATTEMPTS):
            blob = self.bucket.blob(f"{self.prefix}/state.sqlite3")
            try:
                blob.reload(timeout=60)
                generation = int(blob.generation)
            except NotFound:
                generation, content = 0, None
            else:
                try:
                    content = blob.download_as_bytes(if_generation_match=generation, timeout=60)
                except (PreconditionFailed, NotFound):
                    continue
            with tempfile.TemporaryDirectory(prefix="backprop-gcs-") as directory:
                root = Path(directory)
                if content is not None:
                    (root / "backprop.sqlite3").write_bytes(content)
                database = LocalDatabase(root)
                if hasattr(self, "local_id") and database.local_id != self.local_id:
                    raise OSError("The GCS installation identity changed")
                result = operation(database)
                if not write:
                    return result
                backup = root / "snapshot.sqlite3"
                with database.connect() as source, closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                try:
                    blob.upload_from_string(backup.read_bytes(), if_generation_match=generation,
                                            checksum="crc32c", timeout=60)
                    return result
                except PreconditionFailed:
                    pass
            time.sleep(random.uniform(0, min(0.5, 0.01 * 2 ** attempt)))
        raise sqlite3.OperationalError("GCS snapshot contention exceeded the retry deadline")
