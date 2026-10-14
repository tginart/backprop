"""Atomic run-table artifact preparation for logging values.

Architecture: this module creates the actual artifact owner -> Encoder validates
and snapshots one Table with that owner -> stage its immutable artifact manifest -> Run commits the manifest beside the owning
history/config/summary value -> callbacks finalize provenance and table state.
Overwritten pending values never publish artifacts. The native artifact ID is
stable before upload, so offline references need no client/server ID mapping.
Main callers: Run._encoder and Encoder's Table dispatch.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
from urllib.parse import quote
import zlib

from .artifacts import Artifact
from .offline_artifacts import artifact_reference, pending_references_allowed


class PreparedCommits(list):
    """Carry each pending key's manifests, typed-value state and callbacks."""

    def __init__(self, callbacks=(), artifacts=(), tables=None, media=None):
        super().__init__(callbacks)
        self.artifacts = list(artifacts)
        self.tables = dict(tables or {})
        self.media = dict(media or {})


def artifact_name(name):
    """Match pinned InternalArtifact sanitization, including its CRC suffix."""
    clean = re.sub(r"[^a-zA-Z0-9_\-.]+", "", name)
    if clean == name:
        return clean
    suffix = base64.urlsafe_b64encode((zlib.crc32(name.encode()) & 0xFFFFFFFF).to_bytes(4, "big")).rstrip(b"=").decode()
    return clean + "-" + suffix


def prepare_table(run, key, table, descriptor, prior_descriptor=None):
    """Stage one full Table or current increment and return deferred state.

    Main callers: Encoder at a run value boundary. Artifact encoding has its
    own context and never recurses into this helper. Explicit Artifact.add still
    snapshots the whole user object; implicit incremental artifacts store only
    newly logged rows, matching the pinned incremental entry contract.
    """
    # A callable delays schema serialization until its real owner exists.
    # Plain descriptors remain accepted for internal callers with no schema work.
    descriptor_factory = descriptor if callable(descriptor) else lambda artifact: descriptor
    scope = (run.entity, run.project, run.id)
    previous = getattr(table, "_implicit_artifacts", {}).get(scope)
    if previous and (table.log_mode == "IMMUTABLE" or previous["generation"] == table._generation):
        descriptor = descriptor_factory(previous["artifact"])
        reference = artifact_reference(table, run._settings)
        result = {**descriptor, "artifact_path": reference} if reference else dict(descriptor)
        if table.log_mode == "INCREMENTAL":
            result.update(increment_num=previous["increment_num"], previous_increments_paths=previous["previous_increments_paths"])
        return result, [], []
    incremental = table.log_mode == "INCREMENTAL"
    offset = table._offset if incremental else 0
    source = copy.copy(table)
    source._implicit_artifacts = {}
    # The snapshot owns separate logging/provenance state, while schemas may
    # refer to other tables. Those targets stay deferred until this event commits.
    source._snapshot = None
    for attr in ("_artifact_target", "_artifact_target_name"):
        if hasattr(source, attr):
            delattr(source, attr)
    previous_descriptor = prior_descriptor if prior_descriptor is not None else run.summary._data.get(key, {})
    if not isinstance(previous_descriptor, dict):
        previous_descriptor = {}
    if incremental:
        inherited = previous or (previous_descriptor if run.resumed and not previous_descriptor.get("legacy_offline") else {})
        number = inherited.get("increment_num", -1) + 1
        previous_paths = list(inherited.get("previous_increments_paths", []))
        artifact_previous_paths = list(inherited.get("artifact_previous_increments_paths", previous_paths))
        prior_path = inherited.get("artifact_path")
        if prior_path:
            previous_paths.append(prior_path)
        artifact_prior_path = inherited.get("artifact_entry_path", prior_path)
        if artifact_prior_path:
            artifact_previous_paths.append(artifact_prior_path)
        name, kind = artifact_name(f"run-{run.id}-incr-{key}"), "wandb-run-incremental-table"
        entry_name = f"{number}-{time.time_ns() // 1_000_000}.{key}"
        source._increment_num = number
        source._previous_increments_paths = artifact_previous_paths
    else:
        number, previous_paths = None, []
        name, kind, entry_name = artifact_name(f"run-{run.id}-{key}"), "run_table", key
    artifact = Artifact._new_internal(name, kind)
    source_identity = getattr(table, "_tensorboard_record_identity", None)
    if source_identity is not None:
        # Independent importers of the same source record must retain identical
        # chart references even when rich JSON is externalized. This identity
        # applies only to importer-owned, immutable TensorBoard tables.
        if not isinstance(source_identity, str) or not re.fullmatch(r"[a-f0-9]{64}", source_identity) or table.log_mode != "IMMUTABLE":
            raise ValueError("Invalid TensorBoard table source identity")
        artifact._id = hashlib.sha256(json.dumps([*scope, key, source_identity],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    generation = table._generation
    artifact._defer_provenance = True
    descriptor = descriptor_factory(artifact)
    full = descriptor.get("artifact_snapshot", descriptor)
    source.data = list(table.data[offset:offset + len(full["data"])])
    entry = artifact.add(source, entry_name)
    manifest = artifact._prepare_log(run)
    artifact_entry_path = f"backprop-artifact://{quote(artifact._id, safe='')}/{quote(entry.path, safe='/')}"
    reference = artifact_reference(table, run._settings)
    target = getattr(table, "_artifact_target", None)
    if (incremental or table.log_mode == "MUTABLE") and target is not None and target.type in {"run_table", "wandb-run-incremental-table"}:
        reference = None
    if reference is None and pending_references_allowed(run._settings):
        reference = artifact_entry_path
    result = {**descriptor}
    if reference:
        result["artifact_path"] = reference
    if incremental:
        result.update(increment_num=number, previous_increments_paths=previous_paths)
    def commit():
        artifact._commit_log(run, manifest)
        for value, captured in artifact._objects.values():
            if getattr(value, "_artifact_target", None) is None:
                value._artifact_target, value._artifact_target_name = artifact, captured.path
        if getattr(table, "_artifact_target", None) is None or incremental or table.log_mode == "MUTABLE":
            table._artifact_target, table._artifact_target_name = artifact, entry.path
        state = {"generation": generation, "increment_num": number,
                 "previous_increments_paths": previous_paths, "artifact_path": reference,
                 "artifact_entry_path": artifact_entry_path,
                 "artifact_previous_increments_paths": artifact_previous_paths if incremental else [],
                 "artifact": artifact}
        states = dict(getattr(table, "_implicit_artifacts", {}))
        states[scope] = state
        table._implicit_artifacts = states
        if incremental:
            # Explicit public serialization observes only committed increments.
            # Keep Foundry's immutable references available for offline callers.
            table._increment_num = number
            table._previous_increments_paths = list(artifact_previous_paths)
    return result, [manifest], [commit]


def restore_table_types(run, descriptor):
    """Restore resumed constraints with their real owner when custom code needs it.

    Main callers: Run._encode_history_value. Ordinary dtypes need only their
    declarative schema. An explicitly registered custom codec may read constraint
    files, so that path resolves the prior immutable artifact through the existing
    local-journal/authenticated-store boundary before invoking from_json. Missing
    or corrupt references raise before any new row or Table cursor is committed.
    """
    from .tables import ColumnType
    from .table_dtype_bridge import needs_artifact_owner

    from .json_keys import resume_schemas
    schemas = resume_schemas(descriptor)
    owner = None
    if any(needs_artifact_owner(item) for item in schemas):
        from types import SimpleNamespace
        from .artifact_references import native_reference_entry
        from .config_artifacts import JournalArtifactStore
        from .transport import read_journal
        reference = descriptor.get("artifact_path")
        if not isinstance(reference, str) or not reference:
            raise ValueError("Resumed custom Table schema needs its prior artifact reference; sync its source before resuming")
        store = JournalArtifactStore(run.sync_dir, read_journal(run.sync_dir), run._sink)
        owner = native_reference_entry(SimpleNamespace(_store=store), reference).parent_artifact()
    return [ColumnType.from_json(item, owner) for item in schemas]
