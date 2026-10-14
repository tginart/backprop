"""Resolve an offline branch without changing its original durable event log.

Architecture: init writes a branch intent -> first sync freezes the remote
parent -> deterministic replay writes a separate upload log -> its acknowledged
prefix maps back to original event counts. Original events and staged files stay
intact. A failed lookup publishes nothing; retries reuse the frozen parent,
event IDs, timestamps and upload cursor, including ambiguous network outcomes.
Main callers: Run initialization, Journal.flush and read_journal.
Why this exists: an offline process cannot know a remote parent's config or
summary, but must accept the branch and start logging at cutoff + 1 immediately.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid

from .branching import through_step, visible_events
from .metrics import MetricReducer

INTENT = "branch-intent.json"
SNAPSHOT = "branch-parent.json"
PREPARED = "branch-events.jsonl"


def _atomic_json(path, value):
    """Commit one recovery boundary before any corresponding remote writes."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        json.dump(value, target, allow_nan=False, separators=(",", ":"))
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


def create_intent(journal, *, source_id, run_id, project, entity, cutoff, rewind, metadata_overrides):
    """Persist the unresolved branch before Run emits any original records.

    Main callers: offline Run initialization with no local parent. The marker
    contains identifiers and caller choices only, never authentication material.
    """
    intent = {"version": 1, "branch_id": uuid.uuid4().hex, "source_id": source_id,
              "run_id": run_id, "project": project, "entity": entity, "cutoff": cutoff,
              "rewind": rewind, "metadata_overrides": sorted(metadata_overrides)}
    _atomic_json(journal.directory / INTENT, intent)


def unresolved(directory):
    directory = Path(directory)
    return (directory / INTENT).is_file() and not (directory / SNAPSHOT).is_file()


def _raw_events(directory, count=None):
    events = []
    with (Path(directory) / "events.jsonl").open(encoding="utf-8") as source:
        for line in source:
            if count is not None and len(events) >= count:
                break
            if line.strip():
                events.append(json.loads(line))
    return events


def _safe_numbers(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"_type": "number", "value": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
    if isinstance(value, dict):
        return {key: _safe_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_numbers(item) for item in value]
    return value


def _update(target, payload):
    target.update(copy.deepcopy(payload.get("values", {})))
    for key in payload.get("deleted", []):
        target.pop(key, None)
    if "annotation_labels" in payload:
        from .annotation_labels import apply_annotation_update
        apply_annotation_update(target, payload["annotation_labels"])


def _definition(reducer, payload, *, combine=False):
    """Merge an offline declaration with inherited rules using pinned flags.

    Main callers: materialization. Original declarations were made without the
    parent; non-overwrite boolean and summary flags must therefore combine with
    the parent's rule, as they would in an online branch.
    """
    name = payload["name"]
    if combine:
        payload, _ = reducer.prepare_definition(name, **{key: payload[key] for key in
            ("step_metric", "step_sync", "hidden", "summary", "overwrite") if key in payload})
    reducer.definitions[name] = copy.deepcopy(payload)
    reducer.reconfigure(name)
    return payload


def _source_records(events, cutoff):
    """Retain the branch cut plus immutable resources and explicit summaries.

    Config and definitions use their latest parent values, matching an online
    branch. History stays inclusive. Explicit summaries before the first row
    beyond the cut survive; automatic finish snapshots never restore future
    history. Files and artifact manifests remain usable without the parent run.
    """
    visible = visible_events(events)
    selected = {item["event_id"] for item in through_step(visible, cutoff)}
    before_cut = True
    result = []
    for event in visible:
        kind, payload = event["type"], event["payload"]
        if kind == "history" and payload["step"] > cutoff:
            before_cut = False
        if (event["event_id"] in selected or kind in {"file", "artifact", "artifact_use"}
                or kind == "summary" and not payload.get("automatic") and before_cut):
            result.append(event)
    return result


def _replay(events):
    """Rebuild reducer samples, including after a later rewind of this branch.

    Main callers: materialize at first resolution and each subsequent truncate.
    Resetting only the visible summary would keep rejected future samples in
    mean/min/max state, so statistics must be replayed from retained history.
    """
    reducer, config, summary, metadata = MetricReducer(), {}, {}, {}
    for event in events:
        kind, payload = event["type"], copy.deepcopy(event["payload"])
        if kind == "run_start":
            config.update(payload.get("config", {}))
            metadata.update({key: value for key, value in payload.items() if key != "config"})
        elif kind == "run_metadata":
            _update(metadata, payload)
        elif kind == "config":
            _update(config, payload)
        elif kind == "metric_definition":
            _definition(reducer, payload)
        elif kind == "summary":
            _update(summary, payload)
            reducer.explicit_update(payload.get("values", {}), payload.get("deleted", []))
        elif kind == "history":
            reducer.apply(payload["metrics"], summary)
            if "summary" in payload:
                summary = copy.deepcopy(payload["summary"])
    return reducer, config, summary, metadata


def materialize(intent, snapshot, originals):
    """Replay one frozen parent and transform each original event exactly once.

    Main callers: sync preparation and local reads after parent resolution.
    Keeping a one-to-one original suffix makes acknowledgement counts truthful
    for Artifact.wait/status while inherited prefix events have their own cursor.
    """
    selected = _source_records(snapshot["events"], intent["cutoff"])
    reducer, config, summary, metadata = _replay(selected)
    prefix = [] if intent["rewind"] else [
        {**event, "event_id": uuid.uuid5(uuid.UUID(intent["branch_id"]), event["event_id"]).hex,
         "run_id": intent["run_id"], "project": intent["project"], "entity": intent["entity"],
         "payload": copy.deepcopy(event["payload"])} for event in selected]
    inherited_summary = copy.deepcopy(summary)
    result = prefix[:]
    initial_start = True
    for event in originals:
        kind, payload = event["type"], copy.deepcopy(event["payload"])
        if kind == "run_start":
            config.update(payload.get("config", {}))
            payload["config"] = copy.deepcopy(config)
            for key in ("name", "notes", "tags", "group", "job_type"):
                if initial_start and key not in intent["metadata_overrides"] and key in metadata:
                    payload[key] = copy.deepcopy(metadata[key])
            metadata.update({key: value for key, value in payload.items() if key != "config"})
            initial_start = False
        elif kind == "run_metadata":
            _update(metadata, payload)
        elif kind == "config":
            _update(config, payload)
        elif kind == "metric_definition":
            payload = _definition(reducer, payload, combine=True)
        elif kind == "history":
            payload["metrics"] = reducer.synchronize(payload["metrics"])
            reducer.apply(payload["metrics"], summary)
            payload["summary"] = _safe_numbers(copy.deepcopy(summary))
        elif kind == "summary":
            if payload.get("automatic"):
                payload["values"] = _safe_numbers(copy.deepcopy(summary))
                payload.pop("deleted", None)
            else:
                _update(summary, payload)
                reducer.explicit_update(payload.get("values", {}), payload.get("deleted", []))
        elif kind == "history_truncate":
            if intent["rewind"] and initial_start:
                summary = copy.deepcopy(inherited_summary)
            else:
                base = snapshot["events"] if intent["rewind"] else []
                retained = _source_records([*base, *result], payload["step"])
                reducer, _, summary, _ = _replay(retained)
            payload["summary"] = _safe_numbers(copy.deepcopy(summary))
        elif kind == "run_finish":
            payload["summary"] = _safe_numbers(copy.deepcopy(summary))
        result.append({**event, "payload": payload})
    # Preserve copied parent timing. Source clocks may be ahead of the offline
    # machine, so the frozen origin places the child after all source records.
    # Child intervals also remain intact; only equal/backwards timestamps need
    # a one-microsecond ordering adjustment for deterministic reader replay.
    origin = datetime.fromisoformat(snapshot["origin"])
    initial = datetime.fromisoformat(originals[0]["timestamp"]) if originals else origin
    previous = None
    for index, event in enumerate(result):
        original = datetime.fromisoformat(event["timestamp"])
        stamp = original if index < len(prefix) else origin + max(timedelta(0), original - initial)
        if previous is not None:
            stamp = max(stamp, previous + timedelta(microseconds=1))
        event["timestamp"] = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
        previous = stamp
    return result, len(prefix)


def read_local_branch(directory):
    """Read a resolved branch including locally appended, not-yet-synced rows."""
    directory = Path(directory)
    if not (directory / INTENT).is_file() or not (directory / SNAPSHOT).is_file():
        return None
    intent = json.loads((directory / INTENT).read_text())
    snapshot = json.loads((directory / SNAPSHOT).read_text())
    return materialize(intent, snapshot, _raw_events(directory))[0]


def _freeze_parent(directory, intent, originals, sink):
    from .transport import BackpropError
    path = directory / SNAPSHOT
    if path.exists():
        snapshot = json.loads(path.read_text())
        if snapshot.get("branch_id") != intent["branch_id"]:
            raise BackpropError("Offline branch snapshot belongs to a different branch")
        return snapshot
    events = sink.read_events(project=intent["project"], entity=intent["entity"], run_id=intent["source_id"])
    if not events:
        raise BackpropError(f"Offline branch parent {intent['project']}/{intent['source_id']} is not available in Foundry; sync the parent first, then retry this unchanged branch directory")
    if any(event.get("run_id") != intent["source_id"] or event.get("project") != intent["project"]
           or event.get("entity", "") != intent["entity"] for event in events):
        raise BackpropError("Branch parent response includes events outside the requested run")
    if not intent["rewind"] and sink.read_events(project=intent["project"], entity=intent["entity"], run_id=intent["run_id"]):
        raise BackpropError(f"Fork destination run {intent['run_id']} already exists; its events were not changed")
    events = sorted({item["event_id"]: item for item in events}.values(), key=lambda item: (item["timestamp"], item["event_id"]))
    origin = max(datetime.fromisoformat(event["timestamp"]) for event in [*events, originals[0]]) + timedelta(microseconds=1)
    snapshot = {"branch_id": intent["branch_id"], "events": events, "origin": origin.astimezone(timezone.utc).isoformat(timespec="microseconds")}
    _atomic_json(path, snapshot)
    return snapshot


def _write_prepared(directory, events):
    from .transport import BackpropError
    path = directory / PREPARED
    existing = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if len(existing) > len(events) or existing != events[:len(existing)]:
        raise BackpropError("Prepared branch prefix changed; original logs and acknowledgement cursors remain intact")
    if len(existing) == len(events):
        return
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        for event in events:
            target.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


def flush_branch(journal, *, deadline=None, cancel_event=None):
    """Serialize cross-process branch preparation, publish and cursor mapping.

    Main callers: Journal.flush for an unresolved-origin offline run. A file
    lock covers preparation and upload so concurrent sync commands cannot race
    their recovery files. The original append lock is never held for network I/O.
    """
    import fcntl
    from .transport import Journal
    directory = journal.directory
    with (directory / "branch-sync.lock").open("a") as lock:
        while True:
            if cancel_event is not None and cancel_event.is_set() or deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Branch sync deadline/cancellation reached; original events remain queued")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.02)
        try:
            intent = json.loads((directory / INTENT).read_text())
            # Snapshot after acquiring the cross-process lock: another sync
            # may already have prepared a longer prefix while this caller waited.
            # Read only the byte prefix present at entry, so a running producer
            # cannot extend this upload indefinitely. An in-progress final line
            # belongs to the next sync; append fsync completes before Run counts it.
            with journal.lock:
                size = journal.event_path.stat().st_size
                with journal.event_path.open("rb") as source:
                    data = source.read(size)
                lines = data[:data.rfind(b"\n") + 1].splitlines()
                originals = [json.loads(line) for line in lines if line.strip()]
                journal.count = max(journal.count, len(originals))
            if not originals:
                return
            snapshot = _freeze_parent(directory, intent, originals, journal.sink)
            events, prefix_count = materialize(intent, snapshot, originals)
            _write_prepared(directory, events)
            prepared = Journal(directory, journal.sink, _branch_prepared=True)
            try:
                prepared.flush(deadline=deadline, cancel_event=cancel_event)
            finally:
                # Raw acknowledgements count only original events. A crash
                # before this mapping is harmless: the prepared cursor survives.
                raw_ack = max(0, prepared.ack - prefix_count)
                byte_offset = 0
                with journal.event_path.open("rb") as source:
                    for _ in range(raw_ack):
                        byte_offset += len(source.readline())
                with journal.lock:
                    _atomic_json(journal.ack_path, {"count": raw_ack, "byte_offset": byte_offset})
                    journal.ack, journal._byte_offset = raw_ack, byte_offset
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
