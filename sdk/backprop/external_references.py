"""Metadata-only external references, stored in ordinary Foundry manifests.

Architecture: URI dispatch -> pinned provider metadata handler -> immutable
reference descriptor; downloads reopen the original provider with its existing
credential chain and pinned integrity/version checks. Only add_file and native
snapshot copies upload bytes. Main callers: Artifact.add_reference, manifest
entry reads and public artifact File wrappers. Why this exists: checksum=False
must track URI identity without downloading or uploading its content.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

MARKER = '_backprop_external_reference'


def is_external(entry):
    return bool(entry._reference and entry._reference.get(MARKER))


def _handler(uri):
    """Select a known provider before generic HTTPS and unknown-scheme tracking."""
    parsed = urlparse(uri)
    if not parsed.scheme:
        raise ValueError('References must be URIs. To reference a local file, use file://')
    if parsed.scheme == 'file':
        from .reference_handlers.local_file_handler import LocalFileHandler
        return LocalFileHandler()
    if parsed.scheme == 's3':
        from .reference_handlers.s3_handler import S3Handler
        return S3Handler()
    if parsed.scheme == 'gs':
        from .reference_handlers.gcs_handler import GCSHandler
        return GCSHandler()
    if parsed.scheme == 'https' and parsed.netloc.endswith('.blob.core.windows.net'):
        from .reference_handlers.azure_handler import AzureHandler
        return AzureHandler()
    if parsed.scheme in ('http', 'https'):
        from .reference_handlers.http_handler import HTTPHandler
        from .reference_handlers.runtime import HTTPSession
        return HTTPHandler(HTTPSession(), parsed.scheme)
    from .reference_handlers.tracking_handler import TrackingHandler
    return TrackingHandler(parsed.scheme)


def _close(handler):
    cache = getattr(handler, '_cache', None)
    if cache is not None:
        cache.close()
    from .reference_handlers.runtime import HTTPSession
    session = getattr(handler, '_session', None)
    if isinstance(session, HTTPSession):
        session.close()


def add_external_reference(artifact, uri, name=None, checksum=True, max_objects=None):
    """Prepare all entries, then atomically add their provider metadata.

    Main callers: Artifact.add_reference. A failed listing, invalid logical path
    or conflicting entry leaves the destination manifest unchanged. Providers
    retain their exact default/max_objects and checksum=False behavior.
    """
    from .artifacts import _logical_path
    handler = _handler(uri)
    try:
        entries = handler.store_path(artifact, uri, name, checksum, max_objects)
        prepared = []
        for entry in entries:
            path = _logical_path(entry.path)
            reference = {MARKER: True, 'uri': entry.ref}
            candidate = replace(entry, path=path, _parent=artifact, _reference=reference)
            existing = artifact._entries.get(path)
            if existing is not None and (existing.digest != candidate.digest or existing.ref != candidate.ref):
                raise ValueError(f'Artifact entry {path} already contains a different reference')
            prepared.append(candidate)
        artifact._entries.update({entry.path: entry for entry in prepared})
        return prepared
    finally:
        _close(handler)


def read_external_reference(entry):
    """Read through the provider, retaining its source-defined integrity rules.

    Main callers: ArtifactManifestEntry._read_verified_bytes. checksum=False is
    deliberately not a blanket bypass: pinned local/HTTP handlers can reject a
    URI-derived digest when current content/ETag does not match it. Download
    failures propagate before the final user's destination is replaced.
    """
    uri = entry._reference['uri']
    handler = _handler(uri)
    try:
        path = handler.load_path(replace(entry, ref=uri), local=True)
        return Path(path).read_bytes()
    finally:
        _close(handler)
