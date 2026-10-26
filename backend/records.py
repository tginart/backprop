"""
Purpose: Validate canonical event records independently of the storage backend.
Input: The shared Backprop event schema and encoded JSON payload.
Output: Detached canonical records or the shared immutable-identity conflict.
"""
from __future__ import annotations

from datetime import datetime
import json


FIELDS = "schema_version,event_id,run_id,project,entity,event_type,timestamp,payload"
MAX_RECORD_BYTES = 900_000


class RecordConflict(ValueError):
    """The same immutable identity has different content."""


def canonical_record(record: dict) -> tuple[dict, str]:
    """
    Purpose: Validate one event record before any database change.
    Input: The stable tabular Backprop schema.
    Output: A detached record and its canonical identity representation.
    """
    if not isinstance(record, dict) or set(record) != set(FIELDS.split(",")):
        raise ValueError("Invalid Backprop stream record schema")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("Unsupported Backprop event schema version")
    for name in ("event_id", "run_id", "project", "entity", "event_type", "timestamp"):
        if not isinstance(record[name], str) or (name != "entity" and not record[name]):
            raise ValueError("Invalid Backprop event identity")
    datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    if not isinstance(record["payload"], str):
        raise ValueError("Backprop record payload must be encoded JSON")
    payload = json.loads(record["payload"])
    if not isinstance(payload, dict):
        raise ValueError("Backprop event payload must be a JSON object")
    record = {**record, "payload": json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )}
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(canonical.encode()) > MAX_RECORD_BYTES:
        raise ValueError("Backprop event exceeds the 900 KB record limit")
    return record, canonical
