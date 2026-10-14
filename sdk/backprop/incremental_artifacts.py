"""Reconstruct incremental Table prefixes from immutable native artifacts.

Architecture: validate the ordered reference chain -> resolve every entry from
one authenticated manifest snapshot -> verify its bytes -> decode each raw delta
with its own media owner -> append the current delta exactly once. Stored files
remain deltas. W&B accumulates entries in one manifest; Backprop keeps immutable
manifests and records their explicit links instead of editing older resources.
Main callers: object_codec.decode_object for Artifact.get(Table).
"""
from __future__ import annotations

import json
from types import SimpleNamespace


class _ManifestSnapshotStore:
    """Read manifests once while retaining the selected store's byte checks."""

    def __init__(self, store):
        self.store = store
        self.events = store.read_events()

    def read_events(self, project=None, run_id=None, entity=None):
        return [event for event in self.events if
                (project is None or event["project"] == project) and
                (run_id is None or event["run_id"] == run_id) and
                (entity is None or event.get("entity", "") == entity)]

    def download_file(self, reference):
        return self.store.download_file(reference)


def _source_store(artifact):
    """Keep local offline reconstruction inside its owning durable journal.

    Main callers: decode_increment_rows. Remote Artifact objects already carry
    their authenticated store. A locally logged offline artifact instead owns a
    journal with verified staged bytes; reading that journal needs no network.
    """
    owner = getattr(artifact, "_owner", None)
    if artifact._store is None and owner is not None and owner._journal is not None:
        from .config_artifacts import JournalArtifactStore
        from .transport import read_journal
        return JournalArtifactStore(owner.sync_dir, read_journal(owner.sync_dir), None)
    from .artifact_references import _reference_store
    return _reference_store(artifact)


def _validate(value):
    """Reject incomplete or cyclic chains before returning a partial Table."""
    number, paths = value.get("increment_num", 0), value.get("previous_increments_paths", [])
    if type(number) is not int or number < 0:
        raise ValueError("Incremental Table increment_num must be a nonnegative integer")
    if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
        raise ValueError("Incremental Table previous_increments_paths must be native URL strings")
    if len(paths) != number or len(set(paths)) != len(paths):
        raise ValueError("Incremental Table chain is incomplete or repeats an increment")
    columns, data = value.get("columns"), value.get("data")
    if not isinstance(columns, list) or not isinstance(data, list) or any(
            not isinstance(row, list) or len(row) != len(columns) for row in data):
        raise ValueError("Incremental Table rows must match its declared columns")
    return paths


def decode_increment_rows(value, artifact):
    """Return the complete prefix while decoding each delta in its own artifact.

    Main callers: object_codec.decode_object. Prior files are read as raw deltas,
    never recursively as complete Tables, so earlier rows cannot be duplicated.
    Their chain prefixes and increment numbers must agree with the selected
    version. Missing files, changed schemas and checksum failures propagate.
    """
    from .artifact_references import _parse_reference, native_reference_entry
    from .object_codec import decode_object

    paths = _validate(value)
    if not paths:
        return decode_object(value["data"], artifact)
    parsed = [_parse_reference(path) for path in paths]
    if len(set(parsed)) != len(parsed) or any(identity == artifact._id for identity, _ in parsed):
        raise ValueError("Incremental Table chain repeats or cycles to its current artifact")
    lookup = SimpleNamespace(_store=_ManifestSnapshotStore(_source_store(artifact)))
    rows = []
    for number, path in enumerate(paths):
        entry = native_reference_entry(lookup, path)
        try:
            delta = json.loads(entry._read_verified_bytes())
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid incremental Table JSON: {entry.path}") from error
        if (not isinstance(delta, dict) or delta.get("_type") != "table"
                or delta.get("log_mode") != "INCREMENTAL"):
            raise ValueError(f"Increment reference is not an incremental Table: {entry.path}")
        prior_paths = _validate(delta)
        if delta.get("increment_num", 0) != number or prior_paths != paths[:number]:
            raise ValueError(f"Incremental Table chain is out of order: {entry.path}")
        if delta["columns"] != value["columns"]:
            raise ValueError(f"Incremental Table columns changed: {entry.path}")
        rows.extend(decode_object(delta["data"], entry._parent))
    rows.extend(decode_object(value["data"], artifact))
    return rows
