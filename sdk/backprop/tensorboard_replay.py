"""Collapse duplicate imports while preserving every physical writer event.

Architecture: validate versioned source identity -> compare semantic metric
content -> retain the first observation in canonical event order. Main callers:
public run replay; the browser implements the same rule. Why this exists:
separate importers can read a stream before each other's writes are visible.
Only imported history is deduplicated. Ordinary logs and writer accounting stay
unchanged, and disagreement about one source record is an explicit error.
"""
import re

from .integration.tensorboard.origin import validate_record_origin

_MARKERS = {"_tensorboard_record", "_tensorboard_origin", "_tensorboard_source", "_runtime", "_step"}
_PLACEMENT = {"path", "name", "dataset_rid", "transaction_rid", "transaction_id", "_backprop_local_file"}


def semantic_value(value):
    """Keep content, type and metadata, omitting verified file placement only.

    Main callers: TensorBoardHistory.accept and cross-language replay fixtures.
    External JSON retains its byte digest; importer-owned table and artifact
    identities are deterministic so the same rich content has identical bytes.
    Inline table identities can differ, but table cells and schemas cannot.
    """
    if isinstance(value, list):
        return [semantic_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    ignored = set()
    if (isinstance(value.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
            and type(value.get("size")) is int and 0 <= value["size"] <= 2**53 - 1
            and ("path" in value or "_backprop_local_file" in value)):
        ignored.update(_PLACEMENT)
    if value.get("_type") == "table" and "data" in value:
        ignored.update({"table_id", "artifact_path"})
    return {key: semantic_value(item) for key, item in value.items() if key not in ignored}


def _same_value(left, right):
    """Compare JSON values without treating Python booleans as numbers."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


class TensorBoardHistory:
    """One replay-local set; no remote lock, scheduler or persistent cache."""

    def __init__(self, events=()):
        self._seen = {}
        self._rows = {}
        from .tensorboard_group_replay import project_groups
        self._replacements, self._consumed = project_groups(events)

    def rows(self, event):
        """Expose reconciled source groups while retaining physical envelopes."""
        if event['event_id'] in self._rows: return self._rows[event['event_id']]
        if event['event_id'] in self._replacements:
            result = self._replacements[event['event_id']]
        elif event['event_id'] in self._consumed or not self.accept(event): result = []
        else: result = [event['payload'].get('metrics', {})]
        self._rows[event['event_id']] = result
        return result

    def accept(self, event):
        """Return whether to expose this history row, rejecting conflicting data.

        Main callers: reduce_events after physical ordering/writer accounting.
        Older histories without a sidecar remain ordinary rows. A partial or
        malformed sidecar never qualifies as duplicate evidence.
        """
        metrics = event["payload"].get("metrics", {})
        if event["type"] != "history" or "_tensorboard_origin" not in metrics:
            return True
        identifier = validate_record_origin(metrics.get("_tensorboard_record"), metrics["_tensorboard_origin"])
        key = (event.get("entity", ""), event["project"], event["run_id"], identifier)
        content = semantic_value({k: v for k, v in metrics.items() if k not in _MARKERS})
        if key in self._seen:
            if not _same_value(self._seen[key], content):
                raise ValueError(f"Conflicting TensorBoard observations for source record: {identifier}")
            return False
        self._seen[key] = content
        return True
