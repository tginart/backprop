"""Reduce immutable events into read-only experiment snapshots.

Architecture: stable event-ID deduplication -> ordered replay -> metadata,
history, summaries and file manifests. Rich JSON blobs are hydrated on demand;
binary media remains a verified file reference. Main callers: Public Api Run.
"""
from __future__ import annotations

from collections.abc import Mapping
import copy
from datetime import date, datetime
import json

from ...artifact_events import iter_artifact_events


def external_content(value, store, cache):
    """Read verified rich JSON while retaining its file-reference structure.

    Main callers: hydrate and file enumeration. File listing must not decode
    arrays or require NumPy merely to discover a Table's nested media references.
    """
    key = (value.get("dataset_rid"), value["path"], value.get("sha256"))
    if key not in cache:
        cache[key] = json.loads(store.download_file(value))
    content = cache[key]
    if not isinstance(content, dict) or content.get("_type") != value["_type"]:
        raise ValueError("External rich value has a different type than its event descriptor")
    return content


def hydrate(value, store, cache=None, *, external=True):
    """Decode lossless numbers and externally stored rich values recursively."""
    cache = {} if cache is None else cache
    if isinstance(value, list):
        return [hydrate(item, store, cache, external=external) for item in value]
    if not isinstance(value, dict):
        return value
    if external and value.get("_type") == "python.sequence":
        from ...sequence_codec import decode_sequence
        return decode_sequence(value, lambda item: hydrate(item, store, cache))
    if value.get("_type") == "number" and value.get("value") in {"NaN", "Infinity", "-Infinity"}:
        return float(value["value"])
    if value.get("_type") == "timestamp":
        encoded = value["value"]
        return date.fromisoformat(encoded) if len(encoded) == 10 else datetime.fromisoformat(encoded)
    if external and value.get("_type") == "ndarray":
        from ...object_codec import decode_ndarray
        return decode_ndarray(value, hydrate(value["data"], store, cache))
    if external and value.get("external") is True and value.get("_type"):
        content = external_content(value, store, cache)
        return hydrate(content, store, cache)
    return {key: hydrate(item, store, cache, external=external) for key, item in value.items()}


def update_mapping(target, payload):
    """Apply one complete top-level update; nested values are whole snapshots."""
    target.update(copy.deepcopy(payload.get("values", {})))
    for key in payload.get("deleted", []):
        target.pop(key, None)


def flatten_history(values, prefix=""):
    result = {}
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and "_type" not in value:
            result.update(flatten_history(value, name))
        else:
            result[name] = value
    return result


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def reduce_events(events):
    """Replay independent writers without resetting creation time on resume.

    Main callers: Run.load. Stable event IDs remove archive/stream overlap; an
    explicit summary in a history or finish event wins over automatic last-value
    summaries. Metadata and saved files use independent events.
    """
    from ...event_order import ordered_events
    from ...tensorboard_replay import TensorBoardHistory
    ordered = ordered_events(events)
    imported_history = TensorBoardHistory(ordered)
    state = {"metadata": {}, "config": {}, "summary": {}, "history": [], "system": [],
             "files": {}, "artifacts": [], "used_artifacts": [], "console": [],
             "createdAt": None, "updatedAt": None, "state": "running", "definitions": {}, "writers": {}}
    if not any(e['type'] == 'run_start' for e in ordered) and any(e['type'] == 'artifact' and e['payload'].get('logging_disabled') for e in ordered):
        state['state'] = 'disabled'
    snapshots, public_summary, public_deleted = {}, {}, set()
    artifacts = {}
    for output in iter_artifact_events(ordered):
        manifest = output["payload"]
        artifacts.setdefault(manifest.get("artifact_id", output["event_id"]),
                             {"created_at": output["timestamp"], **copy.deepcopy(manifest)})
    for event in ordered:
        payload, kind = event["payload"], event["type"]
        state["updatedAt"] = event["timestamp"]
        writer = payload.get("_writer", {})
        writer_id = writer.get("id", "single")
        if writer:
            state["writers"].setdefault(writer_id, {**writer, "state": "running"})
            if kind in {"run_finish", "writer_finish"}: state["writers"][writer_id]["state"] = payload.get("state", "finished")
        if kind == "run_start":
            if state["createdAt"] is None: state["createdAt"] = event["timestamp"]
            if not writer or writer.get("primary") or not state["metadata"]:
                state["metadata"].update({key: copy.deepcopy(value) for key, value in payload.items() if key not in {"config", "_writer"}})
            state["config"].update(copy.deepcopy(payload.get("config", {})))
            if not writer or writer.get("primary"): state["state"] = payload.get("state", "running")
        elif kind == "run_edit":
            for field in ("metadata", "config"):
                update_mapping(state[field], payload.get(field, {}))
        elif kind == "run_state": state["state"] = payload["state"]
        elif kind == "run_metadata": update_mapping(state["metadata"], payload)
        elif kind in {"config", "summary"}:
            if kind == "summary" and payload.get("public_edit"):
                update_mapping(public_summary, payload)
                public_deleted.difference_update(payload.get("values", {}))
                public_deleted.update(payload.get("deleted", []))
            if kind == "summary" and payload.get("automatic"):
                prior = snapshots.get(writer_id, {})
                state[kind].update({k: copy.deepcopy(v) for k, v in payload.get("values", {}).items() if k not in prior or prior[k] != v})
                snapshots[writer_id] = copy.deepcopy(payload.get("values", {}))
            else: update_mapping(state[kind], payload)
            if kind == "config" and "annotation_labels" in payload:
                from ...annotation_labels import apply_annotation_update
                apply_annotation_update(state["config"], payload["annotation_labels"])
        elif kind == "metric_definition": state["definitions"][payload["name"]] = copy.deepcopy(payload)
        elif kind == "history_truncate":
            state["history"] = [row for row in state["history"] if row["_step"] <= payload["step"]]
            state["summary"] = copy.deepcopy(payload["summary"])
        elif kind in {"history", "system"}:
            visible = imported_history.rows(event)
            if not visible: continue
            for metrics in visible:
                row = flatten_history(copy.deepcopy(metrics))
                row["_step"] = payload.get("step", len(state[kind]))
                if writer:
                    row["_writer"], row["_writer_label"] = writer_id, writer["label"]
                row.setdefault("_timestamp", timestamp(event["timestamp"]))
                if state["createdAt"]:
                    row.setdefault("_runtime", row["_timestamp"] - timestamp(state["createdAt"]))
                state[kind].append(row)
            if kind == "history":
                if "summary" in payload and not writer: state["summary"] = copy.deepcopy(payload["summary"])
                elif "summary" in payload:
                    prior = snapshots.get(writer_id, {})
                    state["summary"].update({k: copy.deepcopy(v) for k, v in payload["summary"].items() if k not in prior or prior[k] != v})
                    snapshots[writer_id] = copy.deepcopy(payload["summary"])
                else: state["summary"].update(copy.deepcopy(payload.get("metrics", {})))
        elif kind == "run_finish":
            state["state"] = payload.get("state", "finished" if not payload.get("exit_code") else "failed")
            if "summary" in payload: state["summary"] = copy.deepcopy(payload["summary"])
        elif kind == "file": state["files"][payload["name"]] = {**copy.deepcopy(payload["file"]), "updated_at": event["timestamp"]}
        elif kind == "artifact_use": state["used_artifacts"].append(copy.deepcopy(payload))
        elif kind == "console": state["console"].append({**copy.deepcopy(payload), "timestamp": event["timestamp"]})
    from ...tensorboard_summary import order_group_rows, grouped_summaries, apply_group_summary
    order_group_rows(state['history'])
    corrections = grouped_summaries(ordered, imported_history)
    if ordered:
        first = ordered[0]
        apply_group_summary(state['summary'], corrections.get((first.get('entity', ''), first['project'], first['run_id'])))
    state["summary"].update(public_summary)
    for key in public_deleted: state["summary"].pop(key, None)
    state["artifacts"] = list(artifacts.values())
    return state


class ReadOnlyMapping(Mapping):
    """Attribute-readable Summary with explicit rejection of remote mutation."""

    def __init__(self, values, store=None, cache=None):
        object.__setattr__(self, "_values", copy.deepcopy(values))
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_cache", {} if cache is None else cache)

    def __getitem__(self, key):
        if isinstance(key, str): key = key.strip()
        value = self._values[key]
        if isinstance(value, dict) and self._store is not None:
            if value.get("_type") in {"numpy.ndarray", "torch.Tensor", "tensorflow.Tensor"} and "file" in value:
                try:
                    import numpy
                except ImportError:
                    raise ImportError("Reading an array from summary requires NumPy") from None
                import io
                return numpy.load(io.BytesIO(self._store.download_file(value["file"])), allow_pickle=False)
            if value.get("_type"):
                value = hydrate(value, self._store, self._cache)
        if isinstance(value, dict): return ReadOnlyMapping(value, self._store, self._cache)
        return hydrate(copy.deepcopy(value), self._store, self._cache)

    def __iter__(self): return iter(self._values)
    def __len__(self): return len(self._values)
    def __contains__(self, key): return (key.strip() if isinstance(key, str) else key) in self._values
    def __getattr__(self, key):
        try: return self[key]
        except KeyError as error: raise AttributeError(key) from error
    def __setattr__(self, key, value): raise TypeError("Public Api summaries are read-only; update from the logging Run")
    def __setitem__(self, key, value): raise TypeError("Public Api summaries are read-only; update from the logging Run")
    def __delitem__(self, key): raise TypeError("Public Api summaries are read-only")
    def update(self, *args, **kwargs): raise TypeError("Public Api summaries are read-only; update from the logging Run")
    def _json_dict(self): return copy.deepcopy(self._values)
