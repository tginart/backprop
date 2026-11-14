"""
Purpose: Keep local copies of immutable bucket objects for all local processes.
Input: An object store and a cache directory.
Output: Local segment files and SHA-256-verified blob bytes.
Note: Bucket objects never change, so a cached copy never becomes stale.
    Atomic renames let viewers, SDK processes and DDP ranks share one cache.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import uuid


DOWNLOAD_WORKERS = 32


class ObjectCache:
    """
    Purpose: Download each immutable object at most once per machine.
    Input: An object store and a directory that this installation owns.
    Output: Paths to local segment copies and verified blob bytes.
    """

    def __init__(self, objects, directory: Path) -> None:
        self.objects = objects
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def fetch_many(self, keys: list[str]) -> dict[str, Path]:
        """
        Purpose: Make local copies of many segment objects in parallel.
        Input: Relative object keys.
        Output: A map from each key to its local file.
        """
        if not keys:
            return {}
        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(keys))) as workers:
            return dict(zip(keys, workers.map(self.fetch, keys)))

    def fetch(self, key: str) -> Path:
        path = self._path("objects", key)
        if not path.is_file():
            self.store(key, self.objects.get(key))
        return path

    def store(self, key: str, data: bytes) -> Path:
        """Save bytes that this process already has, for example its own writes."""
        path = self._path("objects", key)
        _write_atomic(path, data)
        return path

    def read_blob(self, key: str, sha256: str) -> bytes:
        """
        Purpose: Read a content-addressed blob through the cache.
        Input: The blob's object key and its expected SHA-256.
        Output: Bytes whose SHA-256 matches. A bad copy is never returned.
        """
        path = self._path("blobs", sha256)
        if path.is_file():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == sha256:
                return data
        data = self.objects.get(key)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise OSError("Bucket file failed its SHA-256 integrity check")
        _write_atomic(path, data)
        return data

    def _path(self, area: str, key: str) -> Path:
        parts = key.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("Invalid bucket object key")
        return self.directory.joinpath(area, *parts)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
