"""Durable event journal with a small native-Foundry transport boundary.

Architecture: append a complete JSON event locally -> stage referenced files ->
publish a batch -> atomically advance the acknowledged cursor. A retry keeps the
same event IDs, letting readers deduplicate an ambiguous network response.
Main callers: Run initialization, its background flush thread, and CLI sync.
Why this exists: interrupted uploads must remain recoverable without adding a
queue service or application server to Foundry.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import weakref


class BackpropError(Exception):
    """An explicit failure retaining the public Error message/context fields."""

    def __init__(self, message, context=None):
        super().__init__(message)
        self.message = message
        self.context = context or {}


def load_config(path=None):
    """Read the explicit deployment config, then apply documented overrides."""
    path = path or os.environ.get("BACKPROP_CONFIG")
    data = {}
    if path:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
    for field, environment in {
        "host": "BACKPROP_HOST", "stream_dataset_rid": "BACKPROP_STREAM_DATASET_RID",
        "media_dataset_rid": "BACKPROP_MEDIA_DATASET_RID", "app_url": "BACKPROP_APP_URL",
    }.items():
        if os.environ.get(environment):
            data[field] = os.environ[environment]
    return data


def native_store(config, token=None):
    from .foundry import FoundryStore
    if not config.get("host") or not config.get("stream_dataset_rid"):
        raise BackpropError("Online logging requires BACKPROP_CONFIG with host and stream_dataset_rid; use mode='offline' for local logging")
    return FoundryStore(config, token=token)


class MemorySink:
    """Deterministic in-memory backend for tests and embedded integrations."""

    def __init__(self):
        self.events = []
        self.files = {}
        self._artifact_resolutions = {}
        self._artifact_resolution_lock = threading.Lock()

    def __getstate__(self):
        """Keep provided test stores transferable with framework run descriptors.

        Main caller: Lightning/run handoff pickling. The data is serializable;
        the synchronization lock belongs only to its current process.
        """
        state = dict(self.__dict__)
        state.pop("_artifact_resolution_lock", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._artifact_resolution_lock = threading.Lock()

    def read_artifact_resolution(self, key):
        with self._artifact_resolution_lock:
            value = self._artifact_resolutions.get(key)
            return json.loads(json.dumps(value)) if value is not None else None

    def claim_artifact_resolution(self, key, candidate):
        with self._artifact_resolution_lock:
            value = self._artifact_resolutions.setdefault(key, json.loads(json.dumps(candidate)))
            return json.loads(json.dumps(value))

    def publish(self, events):
        self.events.extend(json.loads(json.dumps(events)))

    def read_events(self, project=None, run_id=None, entity=None):
        return [event for event in self.events if
                (project is None or event["project"] == project) and
                (run_id is None or event["run_id"] == run_id) and
                (entity is None or event.get("entity") == entity)]

    def control_reader(self, **scope):
        from .control_store import MemoryControlReader
        return MemoryControlReader(self, **scope)

    def upload_file(self, path, name):
        data = Path(path).read_bytes()
        self.files[name] = data
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "digest": base64.b64encode(hashlib.md5(data).digest()).decode()}

    def download_file(self, reference):
        """Use the same integrity boundary as FoundryStore for local tests."""
        data = self.files[reference["path"]]
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise BackpropError("Downloaded file failed its SHA-256 integrity check")
        if len(data) != reference["size"]:
            raise BackpropError("Downloaded file size does not match its manifest")
        return data


def read_journal(directory, *, project_artifacts=True):
    """Read durable events, optionally retaining original artifact intent IDs.

    Main callers: SDK replay and artifact operation recovery. Public reads use
    canonical prepared identities; retry matching needs the original operation
    that this writer queued, even if another finalizer's manifest won.
    """
    from .deferred_branch import read_local_branch
    prepared = read_local_branch(directory)
    if prepared is not None:
        from .deferred_incremental_artifacts import cached_events
        return cached_events(directory, prepared) if project_artifacts else prepared
    path = Path(directory) / "events.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as source:
        events = [json.loads(line) for line in source if line.strip()]
    from .deferred_incremental_artifacts import cached_events
    return cached_events(directory, events) if project_artifacts else events


class Journal:
    """Serialize uploads and keep failed batches on disk until acknowledged."""

    def __init__(self, directory, sink=None, *, _branch_prepared=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sink = sink
        self.lock = threading.RLock()
        self._upload_lock = threading.Lock()
        self._branch_prepared = _branch_prepared
        self.event_path = self.directory / ("branch-events.jsonl" if _branch_prepared else "events.jsonl")
        self.count = 0
        if self.event_path.exists():
            with self.event_path.open("rb") as source:
                self.count = sum(1 for line in source if line.strip())
        self.ack_path = self.directory / ("branch-ack.json" if _branch_prepared else "ack.json")
        acknowledged = json.loads(self.ack_path.read_text()) if self.ack_path.exists() else {}
        self.ack = acknowledged.get("count", 0)
        self._byte_offset = acknowledged.get("byte_offset", 0)
        if not isinstance(self.ack, int) or not 0 <= self.ack <= self.count:
            raise BackpropError("Invalid journal acknowledgement cursor")
        if self.ack and "byte_offset" not in acknowledged:
            with self.event_path.open("rb") as source:
                for _ in range(self.ack):
                    source.readline()
                self._byte_offset = source.tell()
        self._artifact_projection = {}
        self._uploaded = {}
        self._media_bindings = {}
        self._media_binding_order = 0

    def acknowledged_count(self):
        """Read a durable cursor without changing an active uploader's offsets.

        Main callers: Artifact.state and offline upload status after another
        process runs sync. A finished Run may hold an older in-memory cursor;
        reading this boundary observes the atomic disk acknowledgement while
        preserving the upload lock's ownership of ack and byte_offset.
        """
        with self.lock:
            if not self.ack_path.exists():
                return 0
            acknowledged = json.loads(self.ack_path.read_text(encoding="utf-8"))
            count = acknowledged.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise BackpropError("Invalid journal acknowledgement count")
            available = self.count
            if count > available and self.event_path.exists():
                # A later Run may have appended to this directory after this
                # object finished. Validate against disk only in that case.
                with self.event_path.open("rb") as source:
                    available = sum(1 for line in source if line.strip())
            if count > available:
                raise BackpropError("Journal acknowledgement exceeds its original event count")
            return count

    def append(self, event):
        encoded = json.dumps(event, allow_nan=False, separators=(",", ":"))
        with self.lock:
            with self.event_path.open("a", encoding="utf-8") as target:
                target.write(encoded + "\n")
                target.flush()
                os.fsync(target.fileno())
            self.count += 1

    def stage_file(self, path, *, data=None, name=None):
        """Copy bytes before returning a reference, so caller files may change.

        Main callers: rich value Encoder and Run.log_artifact. The reference
        remains local in the journal and is replaced with a Foundry file
        reference only when publishing. Offline runs can therefore sync later.
        """
        if path is not None:
            source = Path(path)
            name = name or source.name
            digest = hashlib.sha256()
            # Hash exactly the bytes copied, even if a producer edits its
            # source between reads. Never name a copy using a separate read.
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix="staging-", delete=False) as staged:
                temporary = Path(staged.name)
                try:
                    with source.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                            staged.write(chunk)
                    staged.flush()
                    os.fsync(staged.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            identity = digest.hexdigest()
            size = temporary.stat().st_size
        else:
            if data is None:
                raise ValueError("File content is required")
            identity = hashlib.sha256(data).hexdigest()
            size = len(data)
        name = name or identity
        relative = str(Path("files") / identity / Path(name).name)
        destination = self.directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            if path is not None:
                temporary.replace(destination)
            else:
                destination.write_bytes(data)
        if path is not None:
            temporary.unlink(missing_ok=True)
        return {"_backprop_local_file": relative, "name": name, "sha256": identity,
                "size": size, "mime_type": mimetypes.guess_type(name)[0]}

    def stage_live_media(self, media, *, symlink=True):
        """Reuse a value's current source; a new encoded path gets a new binding.

        Main callers: Encoder's scalar media branch. Re-encoding a Video changes
        its source path and must not silently reuse an earlier immutable upload.
        Process-owned temporary sources are copied into the durable journal.
        """
        from .live_media import bind
        key = id(media)
        with self.lock:
            cached = self._media_bindings.get(key)
            source = str(media.path)
            if cached is not None and cached[0]() is media and cached[3] == source:
                return dict(cached[1])
            reference = bind(self.directory, media.path, symlink=symlink, temporary=getattr(media, "_is_tmp", False))
            self._media_bindings[key] = (weakref.ref(media, lambda _: self._media_bindings.pop(key, None)), reference, 0, source)
            return dict(reference)

    def prepare_live_media(self, media, *, symlink=True):
        """Prepare a batch rebind without replacing a committed scalar binding.

        Main callers: Encoder's homogeneous media-list boundary. Its callback
        runs only after the whole row commits, preserving failed-row atomicity.
        """
        from .live_media import bind
        source = str(media.path)
        reference = bind(self.directory, media.path, symlink=symlink, temporary=getattr(media, "_is_tmp", False))
        with self.lock:
            self._media_binding_order += 1
            order = self._media_binding_order
        def commit():
            key = id(media)
            with self.lock:
                prior = self._media_bindings.get(key)
                if prior is None or prior[2] <= order:
                    self._media_bindings[key] = (weakref.ref(media, lambda _: self._media_bindings.pop(key, None)), reference, order, source)
        return reference, commit, order

    def freeze_live_media(self, value):
        """Expose local immutable bytes for a queued linked-media reference."""
        from .live_media import freeze
        return freeze(self, value)

    def _resolve(self, value):
        if isinstance(value, dict):
            if "_backprop_live_media" in value:
                snapshot = self.freeze_live_media(value)
                visible = {key: item for key, item in value.items() if key != "_backprop_live_media"}
                return self._resolve({**snapshot, **visible})
            if "_backprop_local_file" in value:
                relative = value["_backprop_local_file"]
                path = (self.directory / relative).resolve()
                if not path.is_relative_to(self.directory.resolve()):
                    raise BackpropError("Queued file path escapes the run directory")
                key = (relative, value["name"])
                if key not in self._uploaded:
                    digest, size = hashlib.sha256(), 0
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                            size += len(chunk)
                    if digest.hexdigest() != value["sha256"] or size != value["size"]:
                        raise BackpropError("Queued file failed its immutable SHA-256/size check; original event remains unsent")
                    remote_name = f"{value['sha256']}/{value['name']}"
                    uploaded = self.sink.upload_file(path, remote_name)
                    if uploaded.get("sha256") != value["sha256"] or uploaded.get("size") != value["size"]:
                        raise BackpropError("Uploaded file differs from the immutable journal snapshot; original event remains unsent")
                    self._uploaded[key] = uploaded
                resolved = {**{key: item for key, item in value.items() if key != "_backprop_local_file"},
                            **self._uploaded[key]}
                if value.get("mime_type"):
                    resolved["mime_type"] = value["mime_type"]
                if value.get("extra", {}).get("alg") == "XXH128" and value.get("digest"):
                    # Dataset uploads describe their bytes with MD5; artifact
                    # entries can independently declare XXH128 checksums.
                    if self._uploaded[key].get("digest"):
                        resolved["md5"] = self._uploaded[key]["digest"]
                    resolved["digest"] = value["digest"]
                # A file can own other file descriptors, such as an image's
                # segmentation masks. Resolve those before publishing the
                # containing event; a failed child stays in the same journal
                # batch and never leaks a local path to a remote reader.
                return {field: self._resolve(item) for field, item in resolved.items()}
            resolved = {key: self._resolve(item) for key, item in value.items()}
            if resolved.get("_type") in {"table", "plot", "plotly", "histogram"}:
                data = json.dumps(resolved, allow_nan=False, separators=(",", ":")).encode()
                if len(data) > 128_000:
                    staged = self.stage_file(None, data=data, name="rich-value.json")
                    ref = self._resolve(staged)
                    metadata = {key: resolved[key] for key in ("_type", "table_id", "columns", "log_mode", "row_offset", "legacy_offline", "kind", "title") if key in resolved}
                    return {**metadata, **ref, "external": True, "mime_type": "application/json"}
            return resolved
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        return value

    def flush(self, *, deadline=None, cancel_event=None):
        """Upload the current journal prefix without blocking durable appends.

        Main callers: Run background and finish flows, plus offline sync. A
        separate lock serializes network uploads while the short journal lock
        protects byte/count cursors. A deadline bounds waiting and subsequent
        batches; an in-flight HTTP request completes on its own transport timeout.
        """
        if self.sink is None:
            return
        if not self._branch_prepared and (self.directory / "branch-intent.json").is_file():
            from .deferred_branch import flush_branch
            return flush_branch(self, deadline=deadline, cancel_event=cancel_event)
        with self.lock:
            target_count = self.count
        def check_deadline():
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("Journal upload canceled; queued events remain on disk")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Journal upload deadline reached; queued events remain on disk")
        while not self._upload_lock.acquire(timeout=0.1):
            check_deadline()
        try:
            while True:
                check_deadline()
                with self.lock:
                    if self.ack >= target_count:
                        return
                    batch, total = [], 0
                    end_offset = self._byte_offset
                    with self.event_path.open("rb") as source:
                        source.seek(self._byte_offset)
                        for line in source:
                            if not line.strip():
                                end_offset += len(line)
                                continue
                            if batch and (len(batch) >= min(100, target_count - self.ack) or total + len(line) > 512_000):
                                break
                            batch.append(json.loads(line))
                            total += len(line)
                            end_offset += len(line)
                if not batch:
                    raise BackpropError("Journal ended before its event count; refusing to advance the acknowledgement")
                try:
                    if any('_backprop_incremental' in event.get('payload', {}) or '_backprop_distributed' in event.get('payload', {}) for event in batch) or (self.directory / 'incremental-artifacts').exists():
                        from .deferred_incremental_artifacts import prepare
                        with self.event_path.open(encoding='utf-8') as source:
                            originals = [json.loads(line) for line in source if line.strip()][:target_count]
                        self._artifact_projection = {e['event_id']: e for e in prepare(self, originals)}
                    resolved = []
                    for event in batch:
                        check_deadline()
                        resolved.append(self._resolve(self._artifact_projection.get(event["event_id"], event)))
                    check_deadline()
                    self.sink.publish(resolved)
                except TimeoutError:
                    raise
                except Exception as error:
                    raise BackpropError(f"Foundry upload failed; {self.count - self.ack} events remain in {self.directory}. Retry run.flush() or backprop sync. {type(error).__name__}: {error}") from error
                with self.lock:
                    new_ack = self.ack + len(batch)
                    temporary = self.ack_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps({"count": new_ack, "byte_offset": end_offset}), encoding="utf-8")
                    temporary.replace(self.ack_path)
                    self.ack = new_ack
                    self._byte_offset = end_offset
        finally:
            self._upload_lock.release()


def sync(directory, config=None, sink=None):
    """Publish all unacknowledged events in one offline run directory."""
    if not (Path(directory) / "events.jsonl").is_file():
        raise BackpropError("No events.jsonl in the requested directory; pass run.sync_dir to sync an offline run")
    journal = Journal(directory, sink or native_store(config or load_config()))
    journal.flush()
    return journal.ack
