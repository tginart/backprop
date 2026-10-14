"""Expose explicit and value-owned artifacts through one immutable read view.

Architecture: retain input event order -> yield explicit artifact records ->
expand each value event's embedded manifests into deterministic read records.
Synthetic envelopes preserve scope, time and the owning history step; manifest
payloads remain unchanged. This never appends events or rewrites journal data.
Main callers: Public Api, config restoration, native URLs and branch readers.
Why this exists: a Table value and its immutable output must commit atomically,
while artifact readers should not depend on how that output was published.
"""
from __future__ import annotations

import uuid
import json
import re
import copy


_PROVENANCE_FIELDS = {"ref", "birth_artifact_id"}


def manifest_identity(event):
    """Compare immutable content independently of its verified blob location.

    Main callers: artifact event validation and native URL ambiguity checks.
    An uploaded or inherited file can have another dataset/path while retaining
    its logical name, SHA-256, size and artifact checksum. Ignore location only
    when that strong content identity is present; incomplete legacy references
    retain their complete descriptors. File bytes are verified on download.
    """
    payload = {key: value for key, value in event["payload"].items()
               if key not in {"created_at", "history_step", "state", "_writer"}}
    if "files" in payload:
        files = []
        for reference in payload["files"]:
            digest = reference.get("sha256")
            if (isinstance(reference.get("name"), str) and isinstance(digest, str)
                    and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                    and isinstance(reference.get("size"), int) and reference["size"] >= 0):
                location_fields = {"_backprop_local_file", "dataset_rid", "path", "branch", "mime_type", "md5"} | _PROVENANCE_FIELDS
                files.append({key: value for key, value in reference.items() if key not in location_fields})
            else:
                files.append({key: value for key, value in reference.items() if key not in _PROVENANCE_FIELDS})
        payload["files"] = sorted(files, key=lambda item: item.get("name") or item.get("path", ""))
    return json.dumps([event.get("entity", ""), event["project"], payload],
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def merge_manifest_provenance(first, second):
    """Enrich historically absent origin fields without changing content or scope.

    Main callers: iter_artifact_events. A missing field means an older writer
    did not record it. An explicit ``ref: null`` means an ordinary stored file,
    so replacing that with a reference is a conflict. Birth ``null`` remains an
    unknown origin. Two different known birth IDs or references always fail.
    File locations and the envelope come from the first argument, unchanged.
    """
    identity = first["payload"].get("artifact_id", "unknown")
    message = f"Artifact ID is ambiguous: {identity} (conflicting immutable manifests)"
    if manifest_identity(first) != manifest_identity(second):
        raise ValueError(message)
    result = {**first, "payload": copy.deepcopy(first["payload"])}
    source_files = {item.get("name") or item.get("path"): item
                    for item in second["payload"].get("files", [])}
    for target in result["payload"].get("files", []):
        source = source_files[target.get("name") or target.get("path")]
        for field in _PROVENANCE_FIELDS:
            if field not in source or field == "birth_artifact_id" and source[field] is None:
                continue
            if field not in target or field == "birth_artifact_id" and target[field] is None:
                target[field] = source[field]
            elif target[field] != source[field]:
                raise ValueError(message)
    return result


def iter_artifact_events(events):
    """Yield artifact records in their owning events' logical order.

    Main callers: all SDK artifact lookup and replay paths. Repeated event IDs
    from the live stream/archive overlap are collapsed. Different events that
    claim the same scoped artifact ID remain visible for lineage, but their
    immutable content must agree instead of silently selecting one.

    ``source_event_step`` belongs to the envelope, not the manifest: a fork can
    select the owning history row without changing the artifact's identity.
    """
    seen, manifests_by_scope, outputs, parts = {}, {}, [], []
    def scope(event):
        return (event.get("entity", ""), event["project"], event["payload"].get("artifact_id"))
    def checked(event):
        identity = event["payload"].get("artifact_id")
        if identity:
            key = scope(event)
            manifests_by_scope[key] = (merge_manifest_provenance(manifests_by_scope[key], event)
                                       if key in manifests_by_scope else event)
        return event
    for event in events:
        event_id = event["event_id"]
        if event_id in seen:
            if seen[event_id] != event:
                raise ValueError(f"Conflicting records for event ID: {event_id}")
            continue
        seen[event_id] = event
        if event['type'] == 'artifact_part':
            parts.append(event)
        if event["type"] == "artifact":
            outputs.append(checked(event))
            continue
        manifests = event.get("payload", {}).get("artifacts", [])
        if not isinstance(manifests, list):
            raise ValueError("Embedded artifact manifests must be a list")
        identities = {}
        for manifest in manifests:
            if not isinstance(manifest, dict) or not isinstance(manifest.get("artifact_id"), str) or not manifest["artifact_id"]:
                raise ValueError("Embedded artifact manifests require a nonempty artifact_id")
            identity = manifest["artifact_id"]
            if identity in identities:
                if identities[identity] != manifest:
                    raise ValueError(f"Conflicting embedded artifact ID: {identity}")
                continue
            identities[identity] = manifest
            synthetic_id = uuid.uuid5(uuid.NAMESPACE_URL,
                f"backprop:artifact:{event_id}:{identity}").hex
            envelope = {**event, "event_id": synthetic_id, "type": "artifact",
                        "payload": manifest, "source_event_id": event_id,
                        "source_event_type": event["type"]}
            if event["type"] == "history":
                envelope["source_event_step"] = event["payload"]["step"]
            outputs.append(checked(envelope))
    # Derived relationships precede originals so ordinary artifact lookup keeps
    # the actual publication envelope. Alias overlays still use timestamp order.
    from .artifact_lineage import contributor_events
    derived = contributor_events(parts, outputs)
    for event in derived:
        checked(event)
    # Resolve all known origins before yielding the first occurrence. Otherwise
    # a run's first-wins output deduplication could discard a later enrichment.
    for event in [*derived, *outputs]:
        merged = manifests_by_scope.get(scope(event))
        yield merge_manifest_provenance(event, merged) if merged is not None else event
