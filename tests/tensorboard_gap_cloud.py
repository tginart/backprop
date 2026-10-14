"""Offline object-store boundaries for actual TensorBoard gfile tests.

Architecture: genuine event-file bytes populate an in-memory provider -> real
TensorBoard gfile registry/walk/GFile -> native snapshots and real event loader.
Main callers: the optional cloud-source tests. Only provider I/O is replaced;
protobufs, gfile, event loading, conversion, Run.save and history are genuine.
Why this exists: cloud listing/read failures and late visibility can be checked
without credentials, infrastructure or package installations. Authenticated
provider behavior remains a separate verification gap.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
from types import SimpleNamespace
from unittest.mock import patch


class MemoryBlobProvider:
    """Implement TensorBoard's source-backed registered-filesystem boundary.

    Main callers: real tensorflow_stub.io.gfile.walk and GFile. Prefix, listing,
    continuation and binary-read behavior follow the installed TensorBoard
    contract; the object bytes and visibility are owned by each test.
    """

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.reads = []
        self.failure = None

    @staticmethod
    def _text(path):
        return path.decode("utf-8") if isinstance(path, bytes) else path

    def exists(self, path):
        path = self._text(path)
        return path in self.objects or self.isdir(path)

    def isdir(self, path):
        prefix = self._text(path).rstrip("/") + "/"
        return any(name.startswith(prefix) for name in self.objects)

    def join(self, path, *paths):
        return "/".join((self._text(path).rstrip("/"), *paths))

    def listdir(self, path):
        prefix = self._text(path).rstrip("/") + "/"
        if not self.isdir(path):
            raise FileNotFoundError(self._text(path))
        return sorted({name[len(prefix):].split("/", 1)[0] for name in self.objects
                       if name.startswith(prefix)}, reverse=True)

    def stat(self, path):
        return SimpleNamespace(length=len(self.objects[self._text(path)]))

    def read(self, path, binary_mode=False, size=None, continue_from=None):
        """Supply actual GFile reads with an opaque continuation token.

        Main caller: installed TensorBoard GFile. A configured provider error
        escapes immediately; an unavailable source is never an empty byte stream.
        """
        path = self._text(path)
        self.reads.append(path)
        if self.failure is not None:
            raise self.failure
        if path not in self.objects:
            raise FileNotFoundError(path)
        start = (continue_from or {}).get("offset", 0)
        end = len(self.objects[path]) if size is None else start + size
        data = self.objects[path][start:end]
        return (data if binary_mode else data.decode("utf-8"), {"offset": start + len(data)})


@contextmanager
def registered_provider(provider):
    """Use real installed TensorBoard gfile for all pinned cloud URL forms.

    Main caller: optional cloud tests. The registry is restored on exit; a real
    TensorFlow installation does not change which provider boundary this test
    exercises. The native event loader reads the resulting local snapshot.
    """
    from tensorboard.compat.tensorflow_stub.io import gfile
    with patch.dict(gfile._REGISTERED_FILESYSTEMS, {scheme: provider for scheme in ("s3", "gs", "az")}):
        yield gfile


class OfflineGFileBoundary:
    """Minimal cloud-I/O fixture for dependency-free atomic snapshot checks.

    Main caller: portable sources.snapshot tests. This is an object-store I/O
    boundary, not a replacement TensorBoard package or event decoder.
    """

    def __init__(self, data):
        self.data = data
        self.fail_after_first_chunk = False
        self.reads = 0

    def GFile(self, path, mode):
        if mode != "rb":
            raise ValueError("Offline cloud fixture accepts only binary source reads")
        self.reads += 1
        owner = self

        class Source(io.BytesIO):
            def read(self, size=-1):
                if owner.fail_after_first_chunk:
                    if self.tell():
                        raise OSError("provider read interrupted")
                    return super().read(min(size, 4))
                return super().read(size)

        return Source(self.data)
