"""URI-backed Image/Audio values retain provider references inside artifacts.

Architecture: constructors retain the URI without reading it -> direct run
binding rejects the unsupported form -> artifact encoding delegates metadata
and checksums to Artifact.add_reference. Table history carries that same external
entry descriptor; provider bytes are read only on explicit artifact retrieval.
Main callers: Media constructors and Encoder. URI hashes are identity labels,
never checksums sent to the Foundry byte-download validator.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
import weakref


def is_reference(value):
    return isinstance(value, str) and bool(re.match(r"^(gs|s3|https?)://", value))


def initialize(value, uri):
    """Retain the exact URI and pinned SHA256-of-URI identity without I/O."""
    value._reference_uri = uri
    value._path = uri
    value._is_tmp = False
    value._sha256 = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    value.metadata.setdefault("mime_type", mimetypes.guess_type(urlsplit(uri).path)[0])


def descriptor(value, artifact, *, history=False):
    """Create one provider entry using the same actual artifact owner as its Table.

    Main callers: Encoder's Media branch. The manifest owns provider-specific
    metadata and later retrieval; this boundary must not accidentally stage URI
    text or fetch provider bytes as though they were an encoded image/audio file.
    """
    if artifact is None:
        raise ValueError(f"{type(value).__name__} media created by a reference to external storage cannot currently be added to a run")
    uri = value._reference_uri
    name = PurePosixPath(urlsplit(uri).path).name or value.kind
    logical = value.get_media_subdir() + "/" + value._sha256[:20] + "/" + name
    cached = getattr(artifact, "_media_reference_entries", {}).get(id(value))
    entry = artifact._entries.get(logical)
    if not (cached and cached[0]() is value and cached[1]() is entry and entry is not None):
        # A matching filename alone says nothing about storage kind or current
        # provider metadata. New objects go through normal conflict validation.
        # Weak identity cache entries survive a failed outer Artifact.add only
        # as hints: restoring/removing/replacing _entries invalidates them.
        entry = artifact.add_reference(uri, name=logical)[0]
        if not hasattr(artifact, "_media_reference_entries"):
            artifact._media_reference_entries = {}
        artifact._media_reference_entries[id(value)] = (weakref.ref(value), weakref.ref(entry))
    if history:
        # No SHA256 field here: the URI hash is not a checksum of file bytes.
        return {**entry._storage_json(), "mime_type": value.metadata.get("mime_type")}
    return {"artifact_path": logical, "path": logical, "sha256": value._sha256,
            **({"size": entry.size} if entry.size is not None else {})}
