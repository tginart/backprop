"""Editable W&B-shaped run snapshots backed by immutable Foundry events.

Architecture: one scoped event snapshot -> replayed metadata/config/summary ->
history, files and artifact views. Public reads never append events. Reload is
explicit for metadata and automatic for sampled history. Main callers: Api.
"""
from __future__ import annotations

from backprop.errors import UnsupportedFeatureError

import copy
from datetime import datetime
import html
import json
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode

from ..normalize import normalize_exceptions
from .files import File, Files, file_references, like_match
from .display import Displayable
from .history import (ConsoleLogLine, ConsoleLogs, HistoryScan,
                      console_lines, combined_histories, dataframe)
from .query import Collection
from .projects import project_internal_identity
from .mutations import EditableSummary, writer_for
from .replay import ReadOnlyMapping, external_content, hydrate, reduce_events, timestamp


class RunNotFoundError(ValueError):
    pass


class Runs(Collection):
    def __init__(self, factory, per_page=50, *, lazy=True, **kwargs):
        super().__init__(factory, per_page, **kwargs)
        self._lazy = lazy

    @normalize_exceptions
    def histories(self, samples=500, keys=None, x_axis="_step", format="default", stream="default"):
        return combined_histories(self, samples, keys, x_axis, format, stream)

    def upgrade_to_full(self):
        """Switch future reads to full mode and refresh already-listed lazy runs.

        Main callers: Api.runs when a cached lazy collection is requested in full
        mode. Do not fetch an untouched collection just to switch its mode.
        """
        if not self._lazy: return
        self._lazy = False
        for run in self._items or []:
            if run._lazy: run.load_full_data()


class Run(Displayable):
    """An experiment snapshot with event-backed metadata and summary edits."""

    def __init__(self, service_api, entity, project, run_id, attrs=None, include_sweeps=False, lazy=True, api_key=None):
        if include_sweeps:
            raise UnsupportedFeatureError("Sweeps are outside the experiment logging support boundary")
        if api_key is not None and api_key != getattr(service_api, "api_key", None):
            raise ValueError("Configure the Foundry token on Api, not on a returned Run")
        self._api, self._store = service_api, service_api._store
        self._entity, self.project, self._id = entity, project, run_id
        self._lazy = lazy
        self._events, self._state, self._blobs = None, None, {}
        if attrs is not None:
            raise ValueError("Construct runs through Api.run or Api.runs; raw W&B server attrs are not Foundry events")
        if not lazy: self.load()

    @classmethod
    def _from_events(cls, api, entity, project, run_id, events, *, lazy=True):
        result = cls(api, entity, project, run_id)
        result._lazy = lazy
        result._set_events(events)
        return result

    def _set_events(self, events):
        """Validate a complete replacement before publishing any snapshot field.

        Main callers: public construction, refresh, and event-backed mutations.
        Why this exists: replay can reject conflicting delivery, and rich config
        or returned attributes can fail decoding later. Either failure must keep
        raw events, editable values, and their comparison baselines together.
        """
        events = list(events)
        if not any(event["type"] == "run_start" for event in events):
            raise RunNotFoundError("Run not found: " + "/".join(self.path))
        state = reduce_events(events)
        config = {k: v for k, v in hydrate(state["config"], self._store, self._blobs, external=False).items() if k not in {"_wandb", "wandb_version"}}
        replacement = {"_events": events, "_state": state, "_config_edit": config,
                       "_config_before": copy.deepcopy(config),
                       "_metadata_before": copy.deepcopy(state["metadata"])}
        candidate = copy.copy(self)
        candidate.__dict__.update(replacement)
        attrs = candidate._loaded_attrs()
        self.__dict__.update(replacement)
        return attrs

    def load(self, force=False):
        """Refresh the scoped snapshot when requested and return public attributes.

        Main callers: metadata properties and notebook users. Failed reads retain
        the last valid snapshot and propagate the error rather than emptying it.
        """
        if force or self._state is None:
            events = self._store.read_events(entity=self.entity, project=self.project, run_id=self.id)
            writer = getattr(self._api, "_writers", {}).get((self.entity, self.project, self.id))
            return self._set_events([*events, *(writer.published_events() if writer else [])])
        return self._loaded_attrs()

    def _ensure(self):
        if self._state is None: self.load()
        return self._state

    def _record(self):
        state = self._ensure()
        metadata = state["metadata"]
        return {"name": self.id, "displayName": metadata.get("name", self.id),
                "display_name": metadata.get("name", self.id), "entity": self.entity,
                "project": self.project, "group": metadata.get("group"),
                "jobType": metadata.get("job_type"), "host": metadata.get("environment", {}).get("host"),
                "state": state["state"], "tags": metadata.get("tags", []),
                "username": self.entity, "createdAt": state["createdAt"],
                "updatedAt": state["updatedAt"], "heartbeatAt": state["updatedAt"],
                "duration": self.duration, "config": self.config, "summary_metrics": self.summary_metrics}

    def _loaded_attrs(self):
        """Project the loaded snapshot onto public Run attribute names.

        Main callers: load and load_full_data. Query filtering uses _record's
        separate internal naming; returned run data keeps the source's config,
        summaryMetrics and systemMetrics keys. This reads one existing snapshot
        and never opens media blobs or invents W&B account/server identifiers.
        """
        state = self._ensure()
        metadata = state["metadata"]
        environment = metadata.get("environment", {})
        system = state["system"][-1] if state["system"] else {}
        return {"id": self.storage_id, "name": self.id, "projectId": self.project_internal_id,
                "displayName": metadata.get("name", self.id),
                "state": state["state"], "tags": copy.deepcopy(metadata.get("tags", [])),
                "group": metadata.get("group"), "jobType": metadata.get("job_type"),
                "notes": metadata.get("notes"), "description": metadata.get("description"),
                "commit": environment.get("git", {}).get("commit"), "readOnly": self.read_only,
                "createdAt": state["createdAt"], "heartbeatAt": state["updatedAt"],
                "sweepName": None, "config": copy.deepcopy(self.config), "rawconfig": self.rawconfig,
                "summaryMetrics": self.summary_metrics,
                "systemMetrics": hydrate(system, self._store, self._blobs, external=False),
                "historyKeys": self.history_keys, "historyLineCount": len(state["history"])}

    @property
    def id(self): return self._id
    @property
    def read_only(self): return False
    @property
    def entity(self): return self._entity
    @property
    def username(self): return self.entity
    @property
    def storage_id(self): return "/".join(self.path)
    @property
    def name(self): return self._ensure()["metadata"].get("name", self.id)
    @name.setter
    def name(self, value): self._ensure()["metadata"]["name"] = value
    @property
    def state(self): return self._ensure()["state"]
    @property
    def tags(self): return self._ensure()["metadata"].setdefault("tags", [])
    @tags.setter
    def tags(self, value): self._ensure()["metadata"]["tags"] = value
    @property
    def group(self): return self._ensure()["metadata"].get("group")
    @group.setter
    def group(self, value): self._ensure()["metadata"]["group"] = value
    @property
    def job_type(self): return self._ensure()["metadata"].get("job_type")
    @job_type.setter
    def job_type(self, value): self._ensure()["metadata"]["job_type"] = value
    @property
    def notes(self): return self._ensure()["metadata"].get("notes")
    @notes.setter
    def notes(self, value): self._ensure()["metadata"]["notes"] = value
    description = property(lambda self: self._ensure()["metadata"].get("description"),
                           lambda self, value: self._ensure()["metadata"].__setitem__("description", value))
    display_name = property(lambda self: self.name, lambda self, value: setattr(self, "name", value))

    @property
    def created_at(self): return self._ensure()["createdAt"]
    @property
    def updated_at(self): return self._ensure()["updatedAt"]
    @property
    def duration(self):
        state = self._ensure()
        return max(0., timestamp(state["updatedAt"]) - timestamp(state["createdAt"]))
    @property
    def config(self):
        self._ensure()
        return self._config_edit
    @property
    def rawconfig(self):
        from ...code_labels import project_rawconfig
        state = self._ensure()
        return project_rawconfig(hydrate(state["config"], self._store, self._blobs, external=False), state["metadata"])
    @property
    def json_config(self): return json.dumps({key: {"value": value, "desc": None} for key, value in self.rawconfig.items() if key != "wandb_version"})
    @property
    def summary_metrics(self): return hydrate(self._ensure()["summary"], self._store, self._blobs, external=False)
    @property
    def summary(self): return EditableSummary(self)
    @property
    def system_metrics(self):
        rows = self._ensure()["system"]
        return hydrate(rows[-1], self._store, self._blobs, external=False) if rows else {}
    @property
    def path(self): return [self.entity, self.project, self.id]
    @property
    def project_internal_id(self):
        """Stable Backprop project integer; independent of run ID and visibility."""
        return project_internal_identity(self.entity, self.project)
    @property
    def url(self):
        base = self._api._config.get("app_url", "").rstrip("/")
        return base + "/?" + urlencode({"project": f"{self.entity}/{self.project}", "run": self.id})
    @property
    def metadata(self):
        state = self._ensure()
        if "environment" in state["metadata"]:
            return copy.deepcopy(state["metadata"]["environment"])
        reference = state["files"].get("wandb-metadata.json")
        if reference is None: return None
        return json.loads(self._store.download_file(reference))
    @property
    def lastHistoryStep(self):
        self.load(force=True)
        return max((row["_step"] for row in self._state["history"]), default=-1)
    @property
    def history_keys(self):
        """Return metric-key metadata consumed by public Python analysis tools.

        Main callers: history export tools, load and load_full_data. The pinned
        verifier indexes keys/previousValue and lastStep as a mapping despite
        the stale string annotation. Counts/types remain native extra metadata;
        unknown server index fields are not fabricated. Rich values retain their
        saved descriptors and never require binary or external JSON downloads.
        """
        keys = {}
        rows = self._ensure()["history"]
        for row in rows:
            for name, value in row.items():
                if isinstance(value, dict) and value.get("_type"): kind = value["_type"]
                elif value is None: kind = "null"
                elif isinstance(value, bool): kind = "boolean"
                elif isinstance(value, (int, float)): kind = "number"
                elif isinstance(value, str): kind = "string"
                elif isinstance(value, list): kind = "array"
                else: kind = "object"
                entry = keys.setdefault(name, {"count": 0, "types": []})
                entry["count"] += 1
                if kind not in entry["types"]: entry["types"].append(kind)
                entry["previousValue"] = hydrate(value, self._store, self._blobs, external=False)
        return {"keys": keys, "lastStep": max((row["_step"] for row in rows), default=-1)}
    # Sweep properties are installed as explicit unsupported guards.

    @normalize_exceptions
    def history(self, samples=500, keys=None, x_axis="_step", pandas=True, stream="default"):
        """Read sampled metrics using the pinned public argument/error boundary.

        Main callers: charts and analysis notebooks. Key validation happens
        before any read or optional import. Empty keys select complete rows;
        x_axis only applies to a nonempty key selection. Foundry sampling is
        deterministic; scan_history retrieves every selected stored row.
        """
        import backprop
        if keys is not None and not isinstance(keys, list):
            backprop.termerror("keys must be specified in a list")
            return []
        if keys and not isinstance(keys[0], str):
            backprop.termerror("keys argument must be a list of strings")
            return []
        if keys and stream != "default":
            backprop.termerror("stream must be default when specifying keys")
            return []
        if keys:
            rows = self._sampled_history(keys=keys, x_axis=x_axis, samples=samples)
        else:
            rows = self._full_history(samples=samples, stream=stream)
        return dataframe(rows, "pandas", optional=True) if pandas else rows

    def _sampled_history(self, keys, x_axis="_step", samples=500):
        """Project rows with all selected metrics, including the training axis.

        Main caller: history's nonempty-key branch. These validations belong
        at the native query boundary: the pinned client sends later malformed
        keys and axis values to its service instead of issuing a terminal error.
        """
        if not all(isinstance(key, str) for key in keys):
            raise ValueError("keys must be a list of strings")
        if not isinstance(x_axis, str):
            raise ValueError("x_axis must be a string")
        return self._read_history(samples, "history", list(dict.fromkeys([x_axis, *keys])))

    def _full_history(self, samples=500, stream="default"):
        # The pinned helper routes every non-default stream to machine events.
        return self._read_history(samples, "history" if stream == "default" else "system")

    def _read_history(self, samples, stream, keys=None):
        """Refresh once, select stored rows, and hydrate only the returned values.

        Main callers: the two history query branches. Preserve writer-local
        steps and row order; never merge unrelated rows on an equal local step.
        Sampling keeps both endpoints, or the newest row when samples is one.
        Returned values are detached from the cached event snapshot.
        """
        if not isinstance(samples, int) or isinstance(samples, bool) or samples <= 0:
            raise ValueError("samples must be a positive integer")
        self.load(force=True)
        rows = self._state[stream]
        if keys:
            rows = [{key: row[key] for key in keys} for row in rows if all(key in row for key in keys)]
        if len(rows) > samples:
            indices = [len(rows) - 1] if samples == 1 else [round(index * (len(rows) - 1) / (samples - 1)) for index in range(samples)]
            rows = [rows[index] for index in indices]
        return hydrate(rows, self._store, self._blobs)

    @normalize_exceptions
    def scan_history(self, keys=None, page_size=1000, min_step=0, max_step=None, use_cache=True):
        # The cache flag controls reusable immutable file contents. It must
        # never hide newer live events behind an old Run metadata snapshot.
        if not use_cache: self._blobs.clear()
        self.load(force=True)
        rows = self._state["history"]
        maximum = max((row["_step"] for row in rows), default=-1) + 1
        if max_step is not None: maximum = min(max_step, maximum)
        scan = HistoryScan(rows, keys=keys, page_size=page_size, min_step=min_step, max_step=maximum)
        scan._rows = hydrate(scan._rows, self._store, self._blobs)
        return scan

    def beta_scan_history(self, keys=None, page_size=1000, min_step=0, max_step=None, use_cache=True):
        import backprop
        backprop.termwarn(
            "`beta_scan_history` is deprecated and will be removed in a future release. "
            "Use `scan_history` instead.", repeat=False,
        )
        return self.scan_history(keys=keys, page_size=page_size, min_step=min_step,
                                 max_step=max_step, use_cache=use_cache)

    @normalize_exceptions
    def files(self, names=None, pattern=None, per_page=50):
        if names and pattern: raise ValueError("Specify either names or pattern")
        if names is not None and (not isinstance(names, list) or not all(isinstance(name, str) for name in names)):
            raise ValueError("names must be a list of strings")
        if pattern is not None and not isinstance(pattern, str): raise ValueError("pattern must be a string")
        def fetch():
            self.load(force=True)
            state = self._ensure()
            files = dict(state["files"])
            for value in file_references([state["history"], state["summary"]],
                                         lambda item: external_content(item, self._store, self._blobs)):
                filename = (value.get("name") or value["path"]).replace("\\", "/").rsplit("/", 1)[-1]
                name = f"media/{value['sha256']}/{filename}"
                files.setdefault(name, value)
            return [File(self._store, name, ref, self) for name, ref in sorted(files.items())
                    if (not names or name in names) and (pattern is None or like_match(name, pattern))]
        return Files(fetch, per_page)

    @normalize_exceptions
    def file(self, name):
        values = self.files(names=[name])
        if not values: raise ValueError(f"File not found: {name}")
        return values[0]

    @normalize_exceptions
    def logged_artifacts(self, per_page=100):
        from .artifacts import RunArtifacts
        def fetch():
            self.load(force=True)
            unique = {event["payload"]["artifact_id"]: event["payload"]
                      for event in self._api._artifact_events(self.entity, self.project) if event["run_id"] == self.id}
            return [self._api._make_artifact(payload, self.entity, self.project, self.id)
                    for payload in unique.values()]
        return RunArtifacts(fetch, per_page)

    @normalize_exceptions
    def used_artifacts(self, per_page=100):
        from .artifacts import RunArtifacts
        def fetch():
            self.load(force=True)
            # Repeated use calls record relationships, but this API returns
            # distinct artifacts, independent of the number of use_as bindings.
            unique = {}
            for payload in self._ensure()["used_artifacts"]:
                identity = (payload.get("source_entity", self.entity),
                            payload.get("source_project", self.project), payload["artifact_id"])
                unique.setdefault(identity, payload)
            # Names and types can change independently of immutable membership.
            return [self._api.artifact('/'.join(identity)) for identity in unique]
        return RunArtifacts(fetch, per_page)

    def console_logs(self, *, per_page=1000, last=None):
        if last is not None and (not isinstance(last, int) or isinstance(last, bool) or last < 0):
            raise ValueError("last must be a nonnegative integer")
        def fetch():
            self.load(force=True)
            lines = console_lines(self._ensure()["console"])
            selected = lines[-last:] if last else lines if last is None else []
            return selected
        return ConsoleLogs(fetch, per_page, identity=lambda line: str(line.number))

    def load_full_data(self, force=False):
        """Return loaded config, systemMetrics and summaryMetrics attributes.

        Main callers: analyses upgrading a listed Run. Foundry's event snapshot
        already includes these fields. Like the pinned API, a Run returned in
        lazy mode refreshes on every explicit full-data load; a full-mode Run
        reuses its snapshot unless force=True.
        """
        return self.load(force=force or self._lazy)

    @normalize_exceptions
    def wait_until_finished(self):
        while self.state in {"running", "pending"}:
            time.sleep(5)
            self.load(force=True)

    def to_html(self, height=420, hidden=False):
        if not isinstance(height, int) or height <= 0: raise ValueError("height must be a positive integer")
        style = f"border:none;width:100%;height:{height}px;" + ("display:none;" if hidden else "")
        return f'<iframe src="{html.escape(self.url, quote=True)}" style="{style}"></iframe>'

    def download_history_exports(self, download_dir, require_complete_history=True):
        """Export one visible snapshot, with file failures returned explicitly.

        Main callers: analysis notebooks. The existing Foundry event/file store
        supplies data; optional PyArrow writes the native Parquet representation.
        """
        from .history_exports import export_history
        return export_history(self, download_dir, require_complete_history)

    def _readonly(self, *args, **kwargs):
        raise UnsupportedFeatureError("Public Api is read-only; change experiment data through its logging Run")

    delete = _readonly

    @classmethod
    def create(cls, api, run_id=None, project=None, entity=None, state="running"):
        return api._create_run(run_id=run_id, project=project, entity=entity, state=state)

    def _write(self, kind, payload):
        """Persist an edit, then include acknowledged writes in this local view.

        Main callers: edit methods and EditableSummary. Other clients converge
        when Foundry makes the same events visible; this object reads its writes.
        """
        self._ensure()
        pending_config = copy.deepcopy(self._config_edit)
        pending_metadata = copy.deepcopy(self._state["metadata"])
        config_before, metadata_before = self._config_before, self._metadata_before
        writer = writer_for(self._api, self.entity, self.project, self.id)
        writer.write(kind, payload)
        self._set_events([*(self._events or []), *writer.published_events()])
        if kind != "run_edit":
            for before, pending, target in ((config_before, pending_config, self._config_edit),
                                             (metadata_before, pending_metadata, self._state["metadata"])):
                for key in before.keys() - pending.keys(): target.pop(key, None)
                for key, value in pending.items():
                    if key not in before or value != before[key]: target[key] = value
        self._api.flush()

    @normalize_exceptions
    def update(self):
        self._ensure()
        def delta(before, after):
            return {"values": {k: v for k, v in after.items() if k not in before or before[k] != v},
                    "deleted": [k for k in before if k not in after]}
        self._write("run_edit", {"metadata": delta(self._metadata_before, self._state["metadata"]),
                                  "config": delta(self._config_before, self._config_edit)})

    def save(self): self.update()

    @normalize_exceptions
    def update_state(self, state):
        allowed = {"pending": {"running", "failed", "crashed", "preempted"}, "failed": {"pending", "running"}}
        if state not in allowed or self.state not in allowed[state]:
            raise ValueError(f"Cannot transition {self.state} to {state}")
        self._write("run_state", {"state": state})
        return True

    @normalize_exceptions
    def upload_file(self, path, root="."):
        source, base = Path(path).resolve(), Path(root).resolve()
        if not source.is_file() or not source.is_relative_to(base):
            raise ValueError("upload_file path must be a file inside root")
        name = source.relative_to(base).as_posix()
        writer = writer_for(self._api, self.entity, self.project, self.id)
        reference = writer._journal.stage_file(source, name=name)
        self._write("file", {"name": name, "file": reference, "policy": "now"})
        return File(self._store, name, self._state["files"][name], self)

    @normalize_exceptions
    def use_artifact(self, artifact, use_as=None):
        from ...artifacts import Artifact
        if not isinstance(artifact, Artifact) or artifact.is_draft():
            raise ValueError("use_artifact requires an existing committed Artifact")
        self._write("artifact_use", {**artifact._reference_json(), "use_as": use_as or artifact.name})
        return artifact

    @normalize_exceptions
    def log_artifact(self, artifact, aliases=None, tags=None):
        from ...artifacts import Artifact
        if not isinstance(artifact, Artifact) or artifact.is_draft():
            raise ValueError("Public Run.log_artifact requires an existing Artifact")
        if (artifact.entity, artifact.project) != (self.entity, self.project):
            raise ValueError("Cannot log an artifact from another project")
        from ...artifact_updates import validate_aliases, validate_tags
        if aliases is not None: aliases = validate_aliases(aliases)
        if tags is not None: tags = validate_tags(tags)
        # Use the original immutable manifest, never its editable overlay.
        from ...artifact_events import iter_artifact_events
        original = next(e for e in iter_artifact_events(self._store.read_events(entity=self.entity, project=self.project))
                        if e["payload"]["artifact_id"] == artifact.id)
        self._write("artifact", original["payload"])
        if aliases is not None: artifact.aliases = aliases
        if tags is not None: artifact.tags = tags
        if aliases is not None or tags is not None: artifact.save()
        return artifact

    @normalize_exceptions
    def stop(self):
        """Request each observed connected writer to stop once, asynchronously.

        Main callers: remote training control through Api.run. Flush a previous
        failed attempt before inspecting its journal, then retain process UUIDs
        while publishing. Primary completion still owns the final run state.
        A future process or a writer absent from this snapshot is never targeted
        implicitly; delayed visibility can require another explicit stop call.
        """
        from ...control_store import active_stop_controls
        writer = getattr(self._api, '_writers', {}).get((self.entity, self.project, self.id))
        if writer is not None:
            writer.flush()
        self.load(force=True)
        if self.state not in {"running", "pending"}: return
        controls = active_stop_controls(self._events)
        if not controls:
            raise ValueError("Run has no connected cooperative stop handler")
        requested = {event['payload'].get('instance_id') for event in self._events
                     if event['type'] == 'run_stop_request' and event['payload'].get('action') == 'stop'}
        for control in controls:
            if control['instance_id'] not in requested:
                self._write("run_stop_request", {"action": "stop", "instance_id": control["instance_id"]})
    def __repr__(self): return f"<Run {'/'.join(self.path)} ({self.state})>"
    def _repr_html_(self): return self.to_html()
