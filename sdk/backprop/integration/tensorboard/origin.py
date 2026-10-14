"""Source provenance shared by importing and conflict-preserving replay.

Architecture: raw protobuf bytes -> versioned source fields -> recomputable
record identity. Main callers: reader.sync and shared Python/browser replay.
Why this exists: delayed independent importers can observe the same record
before either history is visible. The source identity groups observations;
replay still must compare their actual rich content before collapsing rows.
"""
from __future__ import annotations

import hashlib
import re

_FIELDS = {"version", "namespace", "filename", "event_sha256", "occurrence"}


def record_identity_from_origin(origin):
    """Validate source fields and compute the version-one record identifier.

    Main callers: record_identity, reader.sync and shared replay. Prefix labels
    are separated by NUL bytes; the event digest is appended as its 32 binary
    bytes. Strict types and ranges keep ambiguous or forged sidecars from
    silently becoming trusted duplicate evidence.
    """
    if not isinstance(origin, dict) or set(origin) != _FIELDS:
        raise ValueError("TensorBoard origin must contain exactly the version-one fields")
    if type(origin["version"]) is not int or origin["version"] != 1:
        raise ValueError("TensorBoard origin version must be 1")
    namespace, filename = origin["namespace"], origin["filename"]
    if not isinstance(namespace, str) or "\0" in namespace:
        raise ValueError("TensorBoard origin namespace must be a string without NUL")
    if not isinstance(filename, str) or not filename or filename in {".", ".."} or "/" in filename or "\0" in filename:
        raise ValueError("TensorBoard origin filename must be one nonempty path component")
    digest = origin["event_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("TensorBoard origin event_sha256 must be a lowercase SHA-256 digest")
    occurrence = origin["occurrence"]
    if type(occurrence) is not int or occurrence < 0 or occurrence > 2**53 - 1:
        raise ValueError("TensorBoard origin occurrence must be an exact nonnegative JSON integer")
    prefix = f"{namespace}\0{filename}\0{occurrence}\0".encode("utf-8")
    return hashlib.sha256(prefix + bytes.fromhex(digest)).hexdigest()


def event_origin(namespace, filename, raw, occurrence=0):
    """Describe one duplicate occurrence without including machine paths.

    Main caller: reader.sync. A copied source keeps its namespace, writer
    filename and event digest. Repeated identical events in that source retain
    separate occurrence numbers. The sidecar remains independent of local
    Run.log steps, writer envelopes and media upload paths.
    """
    origin = {"version": 1, "namespace": namespace, "filename": filename,
              "event_sha256": hashlib.sha256(raw).hexdigest(), "occurrence": occurrence}
    record_identity_from_origin(origin)
    return origin


def record_identity(namespace, filename, raw, occurrence=0):
    return record_identity_from_origin(event_origin(namespace, filename, raw, occurrence))


def validate_record_origin(record, origin):
    """Reject provenance whose recomputed identity disagrees with its marker.

    Main callers: durable restart detection and shared replay. Origin metadata
    helps coordinate importers only when it actually describes the claimed
    record; successful validation still does not replace rich-content checks.
    """
    expected = record_identity_from_origin(origin)
    if not isinstance(record, str) or record != expected:
        raise ValueError("TensorBoard record identity does not match its origin")
    return expected


def stabilize_table_ids(metrics, record):
    """Give imported table values identities tied to their source observation.

    Main caller: reader.sync immediately before Run.log. Why this exists:
    independent importers construct different random Table IDs for identical
    PR data, changing external JSON bytes and their integrity digests. A source
    record and metric tag identify that table deterministically, while another
    source occurrence or metric tag keeps its own distinct table identity.
    """
    from ...plot import CustomChart
    from ...tables import Table
    if not isinstance(record, str) or re.fullmatch(r"[0-9a-f]{64}", record) is None:
        raise ValueError("TensorBoard table identity needs a valid record digest")
    for tag, value in metrics.items():
        table = value.table if isinstance(value, CustomChart) else value if isinstance(value, Table) else None
        if table is not None:
            prefix = f"tensorboard-table-v1\0{record}\0{tag}".encode("utf-8")
            identity = hashlib.sha256(prefix).hexdigest()
            table._id = identity[:32]
            table._tensorboard_record_identity = identity


def prior_record_identity(namespace, filename, raw, occurrence=0):
    """Recognize already acknowledged journals from the released importer.

    Main caller: reader.sync during durable restart detection. The previous
    identifier appended the raw event rather than its digest. New observations
    use the recomputable version-one identity; this read-only recognition stops
    a resumed released run from acquiring another copy of its imported data.
    """
    prefix = f"{namespace}\0{filename}\0{occurrence}\0".encode("utf-8")
    return hashlib.sha256(prefix + raw).hexdigest()
