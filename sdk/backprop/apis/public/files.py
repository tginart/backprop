"""Read-only logged files with verified, atomic local downloads.

Architecture: event references -> File metadata -> store download/integrity check
-> atomic destination. Paths are confined to the requested root, including when
a pre-existing symlink attempts to escape it. Main callers: public Run.files.
"""
from __future__ import annotations

from backprop.errors import UnsupportedFeatureError

import base64
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from ..normalize import normalize_exceptions
from .query import Collection
from .display import Displayable


class Files(Collection):
    pass


def safe_destination(root, name):
    """Resolve a logical filename beneath an explicit download directory."""
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError("Invalid logged file name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", name):
        raise ValueError("Logged file paths must be relative")
    directory = Path(root).resolve()
    destination = (directory / name).resolve()
    if not destination.is_relative_to(directory) or destination == directory:
        raise ValueError("Logged file path escapes the download directory")
    return destination


def write_atomic(destination, data):
    """Publish verified bytes, preserving an old destination on write failure."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class File(Displayable):
    """One immutable saved file; URLs require Foundry authentication."""

    def __init__(self, store, name, reference, run=None):
        self._store, self._reference = store, dict(reference)
        self.name, self.run = name, run

    @property
    def id(self):
        scope = "/".join(self.run.path) if self.run is not None else ""
        identity = self._reference.get("sha256") or self._reference.get("uri") or ""
        return hashlib.sha256((scope + "/" + self.name + "/" + identity).encode()).hexdigest()

    @property
    def size(self): return int(self._reference.get("size") or 0)
    @property
    def sizeBytes(self): return self.size
    @property
    def md5(self):
        if self._reference.get("_backprop_external_reference"):
            # Provider ETags and URI identities are not byte MD5 values. Only
            # a verified provider read can supply this public File property.
            if self._reference.get("md5") is None:
                data = self._store.download_file(self._reference)
                self._reference["md5"] = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
            return self._reference["md5"]
        is_xxh128 = self._reference.get("extra", {}).get("alg") == "XXH128"
        digest = self._reference.get("md5") if is_xxh128 else self._reference.get("digest", self._reference.get("md5"))
        if digest is None:
            digest = base64.b64encode(hashlib.md5(self._store.download_file(self._reference), usedforsecurity=False).digest()).decode()
            self._reference["md5" if is_xxh128 else "digest"] = digest
        return digest
    @property
    def mimetype(self): return self._reference.get("mime_type") or "application/octet-stream"
    @property
    def updated_at(self): return self._reference.get("updated_at")
    @property
    def url(self):
        if hasattr(self._store, "file_url"):
            return self._store.file_url(self._reference)
        return "backprop-file://" + self._reference["path"]
    @property
    def direct_url(self): return self.url
    @property
    def path_uri(self): return self.url

    @normalize_exceptions
    def download(self, root=".", replace=False, exist_ok=False, api=None):
        """Return an open text file after an authenticated, verified download.

        Main callers: restore and analysis scripts. This matches W&B's returned
        TextIOWrapper; binary callers can close it and open its .name in rb mode.
        """
        destination = safe_destination(root, self.name)
        if destination.exists() and not replace:
            if exist_ok:
                return destination.open()
            raise ValueError("File already exists; pass replace=True or exist_ok=True")
        store = api._store if api is not None else self._store
        data = store.download_file(self._reference)
        write_atomic(destination, data)
        return destination.open()

    def delete(self):
        raise UnsupportedFeatureError("Public Api file mutation is outside the read-only support boundary")

    def __repr__(self): return f"<File {self.name} ({self.mimetype}) {self.size} bytes>"


def file_references(value, resolve_external=None):
    """Yield media references, opening rich JSON when it contains nested media.

    Main callers: Run.files. An external Table's JSON is itself a logged file,
    and its cells can contain additional images or masks. Binary bytes are never
    downloaded by this enumeration. The caller caches verified JSON reads.
    """
    if isinstance(value, list):
        for item in value: yield from file_references(item, resolve_external)
    elif isinstance(value, dict):
        if "path" in value and "sha256" in value and "size" in value:
            yield value
        if value.get("external") is True and resolve_external is not None:
            yield from file_references(resolve_external(value), resolve_external)
        else:
            for item in value.values(): yield from file_references(item, resolve_external)


def like_match(name, pattern):
    """Translate the documented SQL LIKE wildcard syntax, including escapes."""
    expression, escaped = [], False
    for char in pattern:
        if escaped:
            expression.append(re.escape(char)); escaped = False
        elif char == "\\": escaped = True
        elif char == "%": expression.append(".*")
        elif char == "_": expression.append(".")
        else: expression.append(re.escape(char))
    if escaped: expression.append(re.escape("\\"))
    return re.fullmatch("".join(expression), name, flags=re.DOTALL) is not None
