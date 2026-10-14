"""Resolve native artifact URLs through an existing authenticated store.

Architecture: validate the escaped ID/path -> select the explicit store -> read
immutable manifest events -> reject conflicting ID ownership -> return one
verified entry to Artifact.add_reference's ordinary eager snapshot path.
Main callers: Artifact.add_reference with ArtifactManifestEntry.ref_url output.
Why this exists: a native reference is an identifier, never a network origin.
It must neither bypass Foundry authentication nor silently choose among
conflicting manifests. Repeated copies of the same immutable manifest are safe.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .artifact_events import iter_artifact_events, manifest_identity


def _parse_reference(uri):
    """Decode exactly one quoting layer and validate before reading any store."""
    from .artifacts import _logical_path
    parsed = urlsplit(uri)
    if (parsed.scheme != "backprop-artifact" or not parsed.netloc
            or re.fullmatch(r"[A-Za-z0-9_-]+", parsed.netloc) is None
            or "?" in uri or "#" in uri or not parsed.path.startswith("/")
            or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)):
        raise ValueError("Native artifact references require an ID and a URL-escaped entry path")
    path = _logical_path(unquote(parsed.path[1:], errors="strict"))
    return parsed.netloc, path


def _reference_store(artifact):
    """Use the artifact's store, the active run's store, or configured auth.

    Main callers: native_reference_entry. An active offline/disabled run is an
    explicit no-network boundary, so it never falls through to environment auth.
    Authentication or permission failures propagate from the selected store;
    this function does not retry using a different identity.
    """
    if artifact._store is not None:
        return artifact._store
    import backprop
    active = backprop.run
    if active is not None and not active._closed:
        if active.offline or active.disabled or active._sink is None:
            raise RuntimeError("Native artifact URL lookup requires an authenticated store; offline or disabled runs cannot resolve remote references")
        return active._sink
    from .transport import load_config, native_store
    return native_store(load_config())


def native_reference_entry(artifact, uri):
    """Resolve a native URL without mutating either source or destination.

    Main callers: Artifact.add_reference. Authentication filters the accessible
    event stream before ID matching. The returned entry retains that same store
    for its checksum-verified byte read, then the caller creates an eager copy.
    """
    from .artifacts import Artifact
    # Only this exact query extends immutable references. Keep the strict
    # parser unchanged for incremental-history links and malformed URI checks.
    latest = uri.endswith('?alias=latest')
    immutable_uri = uri[:-len('?alias=latest')] if latest else uri
    identity, path = _parse_reference(immutable_uri)
    store = _reference_store(artifact)
    matches = [event for event in iter_artifact_events(store.read_events())
               if event["payload"].get("artifact_id") == identity]
    if not matches:
        raise ValueError(f"Native artifact ID not found: {identity}")
    if len({manifest_identity(event) for event in matches}) != 1:
        raise ValueError(f"Native artifact ID is ambiguous: {identity}")
    event = matches[0]
    source = Artifact._from_manifest(event["payload"], store, event.get("entity", ""),
                                     event["project"], event["run_id"])
    if latest:
        from .apis.public.api import Api
        source = Api._from_store(store, entity=event.get('entity', ''),
            project=event['project']).artifact(source._name + ':latest')
    if path not in source.manifest.entries:
        raise FileNotFoundError(f"Native artifact entry not found: {path}")
    return source.get_entry(path)
