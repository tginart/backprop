"""Event files -> stable source records -> the existing durable run journal.

Architecture: freeze currently visible source files -> rebuild plugin metadata
and physical source positions -> save those same frozen bytes through Run.save
-> close consecutive same-step groups -> skip durably seen revisions -> log
grouped metrics and full member evidence. Directory
moves do not change identities, while independent writer filenames stay distinct.
Main callers: tensorboard.sync, Run.flush and Api.sync_tensorboard.
Why this exists: a cursor over only the newest filename loses late files and
metadata; rescanning with durable keys also tolerates restart and append retries.
Remote simultaneous writers need replay-level identity deduplication as well.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path, PurePosixPath
import re
import threading

from .codec import values
from .origin import event_origin, record_identity, record_identity_from_origin, validate_record_origin, stabilize_table_ids
from .grouping import closed_groups, history_metrics, validate_group_origin
from .sources import relative_source as _relative, snapshot
from . import sources

_import_lock = threading.RLock()


def _record_keys(events):
    """Trust durable markers only after checking any versioned provenance.

    Main caller: _durable_seen for local journals and visible backend history.
    Released markers without sidecars remain readable during restart, while
    contradictory new metadata raises before it can suppress an actual import.
    """
    keys = set()
    for event in events:
        if event["type"] != "history":
            continue
        metrics = event["payload"].get("metrics", {})
        if "_tensorboard_record" not in metrics:
            continue
        record = metrics["_tensorboard_record"]
        if "_tensorboard_group" in metrics:
            group = metrics["_tensorboard_group"]
            validate_group_origin(record, metrics.get("_tensorboard_origin"), group)
            keys.update(record_identity_from_origin(member["origin"]) for member in [*group["prefix"], *group["members"]])
        elif "_tensorboard_origin" in metrics:
            validate_record_origin(record, metrics["_tensorboard_origin"])
        keys.add(record)
    return keys


def _durable_seen(run):
    """Reconstruct local and visible remote keys before each import pass.

    Main caller: sync. Read errors propagate; a backend failure cannot be treated
    as an empty run and cause a silent duplicate import. Shared writer journals
    are independent, so the backend also supplies records from other machines.
    """
    from ...transport import read_journal
    from datetime import datetime
    from ...tensorboard_group_replay import surviving_history
    seen, groups = set(), set()
    run._tensorboard_seen, run._tensorboard_group_seen = seen, groups
    durable = read_journal(run.sync_dir)
    if run._sink is not None:
        durable.extend(run._sink.read_events(entity=run.entity, project=run.project, run_id=run.id))
    # A local journal and its uploaded copy have different file placement. Keep
    # both immutable source observations for validation instead of comparing the
    # transport envelopes as though their file descriptors were identical.
    durable = surviving_history(sorted(durable, key=lambda event: (
        datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')), event['event_id'])))
    seen.update(_record_keys(durable))
    for event in durable:
        metrics = event["payload"].get("metrics", {})
        if event["type"] == "history" and "_tensorboard_group" in metrics:
            groups.add(validate_group_origin(metrics.get("_tensorboard_record"), metrics.get("_tensorboard_origin"), metrics["_tensorboard_group"]))
    return seen, groups


def _is_event_file(name):
    return bool(re.search(r"tfevents\.\d+", name)) and not name.endswith((".profile-empty", ".sagemaker-uploaded"))


def _files(root):
    """Use installed SDK readers for cloud sources and local directory listings.

    Main caller: sync. Cloud adapters use the existing optional provider SDKs.
    Missing dependencies and provider failures propagate explicitly. Protocol
    fixture evidence does not establish authenticated hosted cloud behavior.
    """
    if "://" not in root:
        path = Path(root)
        if not path.is_dir():
            raise FileNotFoundError(f"TensorBoard root directory does not exist: {root}")
        return sorted(str(item) for item in path.rglob("*tfevents*") if item.is_file() and _is_event_file(item.name))
    with sources.open_filesystem(root) as filesystem:
        if not filesystem.isdir(root):
            raise FileNotFoundError(f"TensorBoard root directory does not exist: {root}")
        return sorted(directory.rstrip("/") + "/" + name
                      for directory, _, names in filesystem.walk(root) for name in names if _is_event_file(name))


def sync(run, root_dir, *, final=None):
    """Import closed consecutive-step groups, preserving all source members.

    Main callers: tensorboard.sync and Api.sync_tensorboard. Historical scans
    close their tail; live polls leave it pending until a different source step
    or final finish. Full rescans rebuild pending groups after restart and allow
    earlier files or same-step appends to revise already closed history. Plugin
    state and per-member table identities follow physical source order.
    """
    from tensorboard.backend.event_processing.event_file_loader import RawEventFileLoader
    from tensorboard.compat.proto.event_pb2 import Event
    from . import _path, source_options, live_filter
    directory = _path(root_dir)
    historical = getattr(run, "_tensorboard_historical_import", False)
    root, save = (directory, True) if historical else source_options(directory, run=run)
    filtering = None if historical else live_filter(run, directory)
    final = historical or filtering is None if final is None else bool(final)
    count = 0
    # Run.log is thread-safe, but the seen/read/log boundary must be serialized
    # across independent local handles as well to avoid a same-process race.
    with _import_lock:
        seen, revisions = _durable_seen(run)
        plugin_streams = {}
        observations = {}
        for path in _files(directory):
            cache = Path(run.sync_dir) / "tmp" / "tensorboard-sources"
            if "://" not in path and Path(path).is_relative_to(cache):
                continue
            if filtering:
                start_time, hostname = filtering
                source = re.search(r"tfevents\.(\d+)\." + re.escape(hostname) + r"(?:\.|$)", path)
                if source is None or int(source.group(1)) < start_time:
                    continue
            relative = PurePosixPath(_relative(path, root))
            namespace = str(relative.parent) if str(relative.parent) != "." else ""
            plugins = plugin_streams.setdefault(namespace, {})
            stream = observations.setdefault(namespace, [])
            occurrences = Counter()
            local_path, local_root = snapshot(run, path, root)
            consumed = 0
            for index, raw in enumerate(RawEventFileLoader(local_path).Load()):
                consumed += 16 + len(raw)
                event = Event.FromString(raw)
                if not event.HasField("summary"):
                    if event.HasField("graph_def") or event.HasField("tagged_run_metadata"):
                        raise NotImplementedError("TensorBoard graph/run metadata has no portable converter")
                digest = hashlib.sha256(raw).digest()
                occurrence = occurrences[digest]
                occurrences[digest] += 1
                origin = event_origin(namespace, relative.name, raw, occurrence)
                key = record_identity_from_origin(origin)
                metrics = values(event.summary, namespace=namespace, plugins=plugins) if event.HasField("summary") else {}
                stabilize_table_ids(metrics, key)
                stream.append({"origin": origin, "index": index, "step": str(event.step),
                               "wall_time": event.wall_time, "metrics": metrics})
            if final and consumed != Path(local_path).stat().st_size:
                raise ValueError("TensorBoard source contains an unreadable or incomplete event record")
            if save:
                run.save(local_path, base_path=local_root, policy="live", glob=False)
        for stream in observations.values():
            for group in closed_groups(stream, final=final):
                if group["revision"] in revisions:
                    continue
                run.log(history_metrics(group), commit=True)
                revisions.add(group["revision"])
                seen.update(record_identity_from_origin(member["origin"]) for member in [*group["prefix"], *group["members"]])
                count += 1
    return count
