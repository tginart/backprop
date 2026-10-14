"""Offline artifact references and legacy table views without a mapping service.

Architecture: finalized typed values retain their artifact provenance -> the
selected run decides whether pending references are allowed -> native immutable
IDs replace W&B client/server ID mapping. Offline legacy table views retain the
full logged artifact/increment beside their capped visible data in one atomic
event. No second upload transaction or mutable artifact alias is introduced.
Main callers: Encoder, Artifact.add/get and WBValue reference helpers.
"""
from __future__ import annotations

from urllib.parse import quote


def current_settings(settings=None):
    """Select the caller's run first, otherwise follow pinned singleton order."""
    if settings is not None:
        return settings
    import backprop
    active = next((run for run in reversed(backprop._runs) if not run._closed), None)
    if active is not None:
        return active._settings
    if backprop._setup_settings is not None:
        return backprop._setup_settings
    from .run import resolve_settings
    return resolve_settings()


def pending_references_allowed(settings=None):
    """Apply the flag only offline; online references are always permitted."""
    selected = current_settings(settings)
    return not selected._offline or selected.allow_offline_artifacts


def artifact_reference(value, settings=None):
    """Return a typed object's immutable source or queued target reference.

    Main callers: Encoder and WBValue._get_artifact_entry_ref_url. A committed
    source is usable even when offline client references are disabled. Targets
    must already be durably logged; native artifacts keep their original ID at
    commit, so a queued reference needs no server-side identity rewrite.
    """
    source = getattr(value, "_artifact_source", None)
    source_name = getattr(value, "_artifact_source_name", None) or getattr(value, "_artifact_content_path", None)
    if source is not None and source_name and source.id is not None:
        return source.get_entry(source_name).ref_url()
    target = getattr(value, "_artifact_target", None)
    target_name = getattr(value, "_artifact_target_name", None)
    if (target is not None and target_name and target._final and target._logged
            and pending_references_allowed(settings)):
        # Validate the captured path through the immutable manifest before
        # publishing a link; stale/corrupt provenance must not create a bad URL.
        entry = target.get_entry(target_name)
        return f"backprop-artifact://{quote(target._id, safe='')}/{quote(entry.path, safe='/')}"
    return None


def latest_artifact_reference(value, settings=None):
    """Name the target collection's latest alias through an immutable anchor.

    Main callers: public Media/Image serialization. The anchor artifact ID
    supplies the authenticated collection namespace; the existing alias reducer
    chooses its latest visible version on each read. A finalized local target can
    expose a pending URL before upload, just as its source SDK client reference.
    """
    target = getattr(value, '_artifact_target', None)
    name = getattr(value, '_artifact_target_name', None)
    if target is None or not name or not target._final or not pending_references_allowed(settings):
        return None
    entry = target.get_entry(name)
    return f"backprop-artifact://{quote(target._id, safe='')}/{quote(entry.path, safe='/')}?alias=latest"


def legacy_table_view(table, descriptor, settings, context):
    """Select the legacy offline file while retaining full artifact recovery.

    Main callers: Encoder._encode_table. Pinned Table.bind_to_run caps its file
    to MAX_ROWS but always logs the larger artifact snapshot too. When client
    references are disabled, incremental files cannot link previous increments:
    each visible value is a standalone snapshot of the latest increment.
    """
    if context not in {"history", "value"} or settings is None or not settings._offline:
        return descriptor
    if settings.allow_offline_artifacts or artifact_reference(table, settings) is not None:
        return descriptor
    data = descriptor["data"]
    count = len(data)
    result = {**descriptor, "data": data[:table.MAX_ROWS], "legacy_offline": True,
              "nrows": len(table.data), "ncols": len(table.columns)}
    if table.log_mode == "INCREMENTAL":
        result.update(table_id=f"{table._id}:legacy:{descriptor['row_offset']}:{count}", row_offset=0)
    if count > table.MAX_ROWS:
        # This is the full current artifact/increment, not an incremental graph.
        # The UI can open/download it separately without filling gaps or joining
        # it into the legacy view. Nested media still uses normal verified refs.
        result["artifact_snapshot"] = {**descriptor, "log_mode": "IMMUTABLE", "row_offset": 0,
            "table_id": f"{table._id}:artifact:{descriptor['row_offset']}:{count}",
            "source_log_mode": table.log_mode, "source_row_offset": descriptor["row_offset"]}
    return result
