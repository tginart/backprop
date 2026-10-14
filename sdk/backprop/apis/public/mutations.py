"""Persist experiment edits without starting or finishing a logging run.

Architecture: public object validates a change -> durable operation journal ->
the existing file uploader and event stream -> normal replay. Upload failures
retain the exact event IDs and staged files for retry through the same object
or the Backprop sync command. No mutable database or separate server is used.
Main callers: public Run, Api creation methods and artifact metadata save.
"""
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
import copy
import os
from pathlib import Path
import threading
import uuid

from ...data_types import Encoder
from ...transport import Journal


class MutationWriter:
    """A scoped durable writer that never emits lifecycle events implicitly.

    Main callers: public API mutations. Why this exists: using a logging Run
    for an edit would falsely mark an existing experiment running/finished.
    """
    def __init__(self, store, entity, project, run_id, *, directory=None):
        self._sink = store
        self.entity, self.project, self.id = entity, project, run_id
        root = Path(os.environ.get("BACKPROP_DIR", str(Path.home() / ".backprop")))
        self._journal = Journal(directory if directory is not None else root / "mutations" / uuid.uuid4().hex, store)
        self._lock = threading.RLock()
        self.step = 0
        self._last_time = datetime.now(timezone.utc)
        self._pending = False

    def _emit(self, kind, payload):
        """Journal one validated event before any network operation.

        Main callers: write and artifact logging. Causal edits must sort after
        the scoped snapshot, including when a training machine's clock is ahead.
        """
        self._last_time = max(datetime.now(timezone.utc), self._last_time + timedelta(microseconds=1))
        self._journal.append({"schema_version": 1, "event_id": uuid.uuid4().hex,
            "timestamp": self._last_time.isoformat(), "entity": self.entity,
            "project": self.project, "run_id": self.id, "type": kind, "payload": payload})
        self._pending = True

    def write(self, kind, payload):
        with self._lock:
            retry = self._pending and getattr(self, "_inflight", None) == (kind, payload)
            self.flush()
            if retry: return
            events = self._sink.read_events(entity=self.entity, project=self.project,
                run_id=None if kind.startswith("artifact_") or kind == "collection_update" else self.id)
            if events:
                self._last_time = max(self._last_time, *(datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) for e in events))
            encoder = Encoder(self._journal.stage_file)
            encoded = encoder.encode(payload)
            self._inflight = (kind, copy.deepcopy(payload))
            self._emit(kind, encoded)
            encoder.commit()
            self.flush()

    def published_events(self):
        from ...transport import read_journal
        return [self._journal._resolve(event) for event in read_journal(self._journal.directory)]

    def flush(self):
        if self._pending:
            self._journal.flush()
            self._pending = False


def writer_for(api, entity, project, run_id):
    """Reuse pending journals on retries within the same Api instance."""
    writers = getattr(api, "_writers", None)
    if writers is None:
        api._writers = writers = {}
    key = entity, project, run_id
    if key not in writers:
        writers[key] = MutationWriter(api._store, *key)
    return writers[key]


class EditableSummary(Mapping):
    """Nested summary views publish only their edited root key.

    Main callers: public Run.summary. Reads reuse the existing rich-value
    decoder; writes never send a stale full summary over another writer's keys.
    """
    def __init__(self, run, path=()):
        object.__setattr__(self, "_run", run)
        object.__setattr__(self, "_path", path)

    def _mapping(self):
        value = self._run.summary_metrics
        for key in self._path:
            value = value[key]
        return value

    def __getitem__(self, key):
        from .replay import ReadOnlyMapping
        value = self._mapping()[key]
        if isinstance(value, dict) and "_type" not in value:
            return EditableSummary(self._run, (*self._path, key))
        return ReadOnlyMapping({key: value}, self._run._store, self._run._blobs)[key]

    def __iter__(self): return iter(self._mapping())
    def __len__(self): return len(self._mapping())
    def keys(self): return self._mapping().keys()
    def items(self): return [(key, self[key]) for key in self]
    def get(self, key, default=None): return self[key] if key in self._mapping() else default
    def __getattr__(self, key):
        try: return self[key]
        except KeyError as error: raise AttributeError(key) from error
    def __setattr__(self, key, value): self[key] = value
    def __setitem__(self, key, value): self._change({key: value}, ())
    def __delitem__(self, key):
        if key not in self._mapping(): raise KeyError(key)
        self._change({}, (key,))

    def update(self, values=None, **kwargs):
        self._change({**dict(values or {}), **kwargs}, ())

    def _json_dict(self): return self._mapping()

    def _change(self, values, deleted):
        import copy
        if self._path:
            root_key = self._path[0]
            root = copy.deepcopy(self._run.summary_metrics[root_key])
            target = root
            for key in self._path[1:]: target = target[key]
            target.update(values)
            for key in deleted: del target[key]
            values, deleted = {root_key: root}, ()
        self._run._write("summary", {"values": values, "deleted": list(deleted), "public_edit": True})
