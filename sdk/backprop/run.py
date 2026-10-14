"""Single-process Run lifecycle, atomic history and Foundry persistence.

Architecture: resolve settings/identity -> replay prior events -> bind config and
summary -> validate before changing pending rows -> durable journal -> background
uploads. Metadata updates are independent of run starts. finish commits pending
history, snapshots files and resets module globals only after terminal records.
Created by backprop.init and used by training programs and logging observers.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import numbers
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import quote, parse_qs, urlsplit
import uuid
import warnings
from functools import wraps

from collections.abc import Mapping
from .config import Config, ConfigStatic, parse_config
from .branching import through_step, visible_events
from .data_types import Artifact, Encoder, Table
from .errors import Error, UsageError
from .failures import retryable_failure
from .file_logging import RunFiles, RunStatus, restore_file
from .disabled import Noop
from .run_calls import run_method
from .metrics import MetricReducer, Metric
from .settings import Settings
from .summary import Summary
from .runtime import ConsoleCapture, bounded_call, report_background_warning
from .run_metadata import save_metadata
from .transport import BackpropError, Journal, load_config, native_store, read_journal
from .settings_paths import workspace_dir
from .debug_logs import DebugLogs


def _cleanup_failed_initialization(function):
    """Release per-run handlers/capture if any initialization stage raises.

    Main callers: Run.__init__. Its public signature remains inspectable through
    wraps; cleanup never marks a partially initialized journal as finished.
    """
    @wraps(function)
    def initialize(self, *args, **kwargs):
        self._debug = None
        self._notebook = None
        self._stop_checker = None
        self._tensorboard_registered = False
        try:
            return function(self, *args, **kwargs)
        except BaseException:
            stop = getattr(self, "_stop", None)
            if stop is not None:
                stop.set()
            wake = getattr(self, "_wake", None)
            if wake is not None:
                wake.set()
            try:
                if self._stop_checker is not None:
                    self._stop_checker.stop_soon()
                if self._notebook is not None:
                    self._notebook.close()
                console = getattr(self, "_console", None)
                if console is not None:
                    console.stop()
            finally:
                try:
                    self._release_tensorboard()
                finally:
                    if self._debug is not None:
                        self._debug.close()
            raise
    return initialize


def resolve_settings(settings=None):
    result = Settings()
    setup = getattr(sys.modules.get("backprop"), "_setup_settings", None)
    for source in (setup, settings):
        values = {key: source._values[key] for key in source._explicit} if isinstance(source, Settings) else source or {}
        if not isinstance(values, dict):
            raise TypeError("settings must be Settings or a dictionary")
        result.update_from_dict({key: values[key] for key in ("root_dir", "settings_system", "use_dot_wandb") if key in values})
    if result.settings_system is None:
        result.settings_system = str(Path(os.environ.get("WANDB_CONFIG_DIR", str(Path.home()/".config/wandb"))) / "settings")
    result.update_from_system_settings()
    result.update_from_env_vars(os.environ)
    # Discover paths only after init has applied its final privacy/mode values.
    # Run metadata and notebook setup perform that later lifecycle stage.
    from .settings_environment import refresh_environment
    refresh_environment(result, discover=False)
    if setup is not None:
        result.update_from_settings(setup)
    if settings is not None:
        if isinstance(settings, Settings):
            result.update_from_settings(settings)
        elif isinstance(settings, dict):
            result.update_from_dict(settings)
        else:
            raise TypeError("settings must be Settings or a dictionary")
    if os.environ.get("BACKPROP_MODE") and not (settings and "mode" in (settings._explicit if isinstance(settings, Settings) else settings)):
        result.mode = os.environ["BACKPROP_MODE"]
    return result


class Run:
    """One run with W&B-shaped public properties and atomic local persistence."""

    @_cleanup_failed_initialization
    def __init__(self, settings=None, config=None, sweep_config=None, launch_config=None, *,
                 project=None, entity=None, name=None, id=None, tags=None, group=None,
                 job_type=None, notes=None, dir=None, mode=None, resume=None,
                 _sink=None, _deployment=None, _shared_writer=False, _event_time_floor=None,
                 _prepared_auth=None, _tensorboard_nonglobal_init=False):
        self._frozen = False
        # Public create_new handles retain adapter hook ownership but must not
        # import directories announced to the global run. Direct Run handles
        # keep their explicit automatic-import behavior.
        self._tensorboard_nonglobal_init = _tensorboard_nonglobal_init
        self._warnings = []
        self._sparklines = {}
        self._settings = resolve_settings(settings)
        updates = {"project": project, "entity": entity, "run_name": name, "run_id": id,
                   "run_tags": tags, "run_group": group, "run_job_type": job_type, "run_notes": notes,
                   "root_dir": os.fspath(dir) if dir is not None else None, "mode": mode, "resume": resume}
        self._settings.update_from_dict({k: v for k, v in updates.items() if v is not None})
        from .notebook_discovery import resolve_notebook
        resolve_notebook(self._settings)
        if self._settings.program is None:
            from .settings_environment import launch_program
            self._settings.program = launch_program(self._settings) or "<python with no main file>"
        from .settings_policy import validate_logging_settings
        for message in validate_logging_settings(self._settings):
            self._warn(message)
        supplied_settings = self._settings._explicit.copy()
        self._shared = bool(_shared_writer) or self._settings.mode == "shared"
        self._writer_id = uuid.uuid4().hex
        self._writer_sequence = 0
        self._last_event_time = datetime.now(timezone.utc)
        if _event_time_floor is not None:
            self._last_event_time = max(self._last_event_time, datetime.fromisoformat(_event_time_floor))
        if self._shared and (self._settings.resume_from or self._settings.fork_from):
            raise UsageError("Shared runs cannot fork or rewind another writer's history")
        self._settings.project = self._settings.project or "uncategorized"
        self._settings.entity = self._settings.entity or ""
        if any(c in self.entity for c in "/\\"):
            raise UsageError("Entity cannot contain path separators")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._background_enabled = False
        self._error = None
        self._closed = False
        self._pending = {}
        self._pending_commits = {}
        self._step = 0
        self._starting_step = 0
        self._started = time.time()
        self._last_heartbeat = time.monotonic()
        self._init_deadline = time.monotonic() + self._settings.init_timeout
        self._settings._start_time = self._started
        self._unhandled_exit_code = 0
        self._sync_time = None
        self._reducer = MetricReducer()
        self._definitions, self._last, self._stats = self._reducer.definitions, self._reducer.last, self._reducer.stats
        self._deployment = dict(_deployment if _deployment is not None else load_config())
        self._deployment.update({name: getattr(self._settings, name) for name in
                                 ("http_proxy", "https_proxy", "insecure_disable_ssl")
                                 if name in self._settings._explicit})
        self._deployment["timeout"] = min(self._deployment.get("timeout", 60), self._settings.init_timeout)
        if "base_url" in supplied_settings:
            host = urlsplit(self._settings.base_url).hostname or ""
            if host.endswith(("wandb.ai", "wandb.com")):
                raise UsageError("Backprop settings.base_url must be a Foundry origin; omit it to use BACKPROP_CONFIG")
            self._deployment["host"] = self._settings.base_url.rstrip("/")
        self._settings._app_url = self._deployment.get("app_url")
        token = self._settings.api_key
        if _sink is None and self.mode in {"online", "shared"}:
            from .login_session import authenticate_candidate, persist_candidate
            # Public init has already selected auth before its active-run policy;
            # direct Run callers select here. Consuming its detached candidate
            # avoids a second prompt or a changed credential source after reinit.
            candidate = _prepared_auth if _prepared_auth is not None else authenticate_candidate(
                key=token, host=self._deployment.get('host'), prompt=True,
                verify=False, timeout=None, settings=self._settings,
                load_config=lambda: self._deployment, native_store=native_store)
            if candidate.mode != 'online':
                self._settings.mode = candidate.mode
                self._shared = bool(_shared_writer)
            else:
                token = candidate.token
                self._settings.api_key = token
                if candidate.source == 'prompt': persist_candidate(candidate, self._settings)
        self._sink = _sink if _sink is not None else (native_store(self._deployment, token=token) if self.mode in {"online", "shared"} else None)
        self._journal = None
        root = Path(dir or os.environ.get("BACKPROP_DIR") or self._settings.root_dir).expanduser().resolve()
        base = workspace_dir(root, self._settings.use_dot_wandb)
        project_key = hashlib.sha256(f"{self.entity}/{self.project}".encode()).hexdigest()[:12]
        self._auto_resume_path = base / project_key / "resume.json"
        self._settings._resume_fname = str(self._auto_resume_path)
        self._settings._wandb_dir = str(base)
        explicit_id = self._settings.run_id
        selected_resume = self._settings.resume
        if self.offline and selected_resume:
            self._warn("resume is ignored in offline mode; starting a new local run with this ID")
            selected_resume = None
        moment = self._settings.resume_from or self._settings.fork_from
        rewinding = self._settings.resume_from is not None
        if rewinding:
            if explicit_id is not None and explicit_id != moment.run:
                raise UsageError("resume_from run ID must match id")
            self._settings.run_id = moment.run
            explicit_id = moment.run
        if selected_resume == "auto" and not explicit_id and self._auto_resume_path.is_file():
            self._settings.run_id = json.loads(self._auto_resume_path.read_text())["run_id"]
        self._settings.run_id = self._settings.run_id or uuid.uuid4().hex[:8]
        if self._settings.fork_from and moment.run == self.id:
            raise UsageError("fork_from run ID must be different from the new run ID")
        if not self.offline:
            self._settings.run_name = self._settings.run_name or self.id
        self._sync_dir = str(base / project_key / self.id)
        if self.offline and (Path(self._sync_dir) / "events.jsonl").exists():
            self._sync_dir = str(base / project_key / f"{self.id}-offline-{uuid.uuid4().hex[:8]}")
        if self._shared:
            self._sync_dir = str(base / project_key / self.id / "writers" / self._writer_id)
        self._settings._sync_dir = self._sync_dir
        self._files_dir = str(Path(self._sync_dir) / "files")
        self._files = RunFiles(self)
        self._config = Config()
        self._config._bind(self, self._settings)
        self.summary = Summary(self)
        self._resumed = False
        previous = []
        resumed_parent = False
        if not self.disabled:
            source_id = moment.run if moment else self.id
            source_dir = base / project_key / source_id
            from .deferred_branch import unresolved, create_intent
            pending_parent = unresolved(source_dir)
            if pending_parent and not self.offline:
                raise BackpropError("The local parent branch is unresolved; sync its directory before initializing online")
            local = read_journal(source_dir) if (not self.offline or moment) and not pending_parent else []
            remote = self._initial_call(lambda: self._sink.read_events(project=self.project, run_id=source_id, entity=self.entity)) if self._sink is not None and not self.offline and (not self._shared or selected_resume == "must") and (explicit_id or selected_resume or moment) else []
            previous = sorted({e["event_id"]: e for e in [*local, *remote]}.values(), key=lambda e: (e["timestamp"], e["event_id"]))
            previous = visible_events(previous)
            if selected_resume == "must" and not previous:
                raise BackpropError(f"Cannot resume missing run {self.project}/{self.id}")
            deferred_branch = bool(moment and not previous and self.offline)
            if moment and not previous and not deferred_branch:
                raise BackpropError(f"Cannot branch missing run {self.project}/{source_id}")
            if previous and not self._shared and not moment and selected_resume in {None, False, "never"}:
                raise BackpropError(f"Run {self.id} exists; use resume='allow' or resume='must'")
            if moment and self._settings.fork_from:
                destination = read_journal(self._sync_dir)
                if self._sink is not None and explicit_id and not self.offline:
                    destination += self._initial_call(lambda: self._sink.read_events(project=self.project, run_id=self.id, entity=self.entity))
                if destination:
                    raise BackpropError(f"Fork destination run {self.id} already exists")
            self._journal = Journal(self._sync_dir, None if deferred_branch else self._sink)
            if deferred_branch:
                overrides = {field for field, setting in (("name", "run_name"), ("notes", "run_notes"),
                    ("tags", "run_tags"), ("group", "run_group"), ("job_type", "run_job_type")) if setting in supplied_settings}
                create_intent(self._journal, source_id=source_id, run_id=self.id, project=self.project,
                              entity=self.entity, cutoff=int(moment.value), rewind=rewinding, metadata_overrides=overrides)
                self._warn("Offline parent is unavailable locally; sync will resolve the parent before publishing this branch")
            Path(self._files_dir).mkdir(parents=True, exist_ok=True)
            self._debug = DebugLogs(self)
            self._debug.start()
        if not self.disabled and not self._shared and not moment:
            from .run_handoff import restore_parent_identity
            resumed_parent = restore_parent_identity(self, local)
        if self._shared and not resumed_parent:
            previous = []  # A new writer has independent steps, config deltas and reducers.
        elif resumed_parent:
            previous = [event for event in previous if not event["payload"].get("_writer")
                        or event["payload"]["_writer"]["id"] == self._writer_id]
        self._resumed = bool(previous) and not moment
        self._settings.resumed = self._resumed
        replay = through_step(previous, int(moment.value)) if moment else previous
        if moment and not rewinding:
            # Restore summaries/table state from the same child-owned snapshots
            # that inherited history uses, including deferred linked media.
            replay = [{**event, "payload": self._copy_branch_files(event["payload"], source_dir)} for event in replay]
        for event in replay:
            self._restore(event)
        self._tensorboard_replay_active = any('_tensorboard_group' in event['payload'].get('metrics', {}) for event in replay)
        if self._tensorboard_replay_active:
            from .tensorboard_summary import run_correction, apply_group_summary
            correction = run_correction(self, replay)
            apply_group_summary(self.summary._data, correction)
            self._reducer = correction['reducer']
            self._definitions, self._last, self._stats = self._reducer.definitions, self._reducer.last, self._reducer.stats
        if moment:
            self._step = int(moment.value) + 1
        self._starting_step = self._step
        if previous:
            metadata = {}
            for event in previous:
                if event["type"] == "run_start":
                    metadata.update(event["payload"])
                elif event["type"] == "run_metadata":
                    metadata.update(event["payload"].get("values", {}))
            for field, setting, given in [("name", "run_name", name), ("notes", "run_notes", notes), ("tags", "run_tags", tags),
                                          ("group", "run_group", group), ("job_type", "run_job_type", job_type)]:
                if setting not in supplied_settings and given is None and field in metadata:
                    setattr(self._settings, setting, metadata[field])
        initial = {}
        for path in self._settings.config_paths or ():
            initial.update(parse_config(path))
        initial.update({**parse_config(config), **parse_config(sweep_config), **parse_config(launch_config)})
        if initial:
            self._config._data.update(self._config._prepare(initial, allow_val_change=True))
        if previous:
            from .config_artifacts import hydrate_config
            object.__setattr__(self._config, "_data", hydrate_config(self, previous, source_dir))
        if not self.disabled and moment and not rewinding:
            self._inherit_config_artifacts(previous, source_dir)
        encoder = self._encoder()
        self._prepare_config_artifacts(self.config._as_dict())
        encoded_config = self.config._as_dict() if self.disabled else encoder.encode(self.config._as_dict())
        if moment and rewinding:
            self._emit("history_truncate", {"step": int(moment.value), "summary": encoder.encode(self.summary._data)})
        elif moment:
            for event in replay:
                if event["type"] in {"history", "metric_definition"}:
                    self._emit(event["type"], event["payload"])
        from .run_stopping import RunStopChecker
        self._stop_checker = RunStopChecker(self)
        from .code_labels import probe_code_labels
        self._code_labels = probe_code_labels(self)
        self._emit("run_start", {**({"control": self._stop_checker.capability} if self._stop_checker.reader is not None else {}), **self._metadata(), "config": encoded_config, "resumed": self.resumed,
                                 **({"forked": True, "fork_from": {"run": moment.run, "step": int(moment.value)}} if moment else {}),
                                 "state": "running", "mode": self.mode, "code_labels": self._code_labels,
                                 **({"artifacts": encoder.artifacts} if encoder.artifacts else {})})
        encoder.commit()
        if not self.disabled:
            save_metadata(self)
            self._debug.user(f"Run initialized: {self.project}/{self.id} ({self.mode})")
        if selected_resume == "auto" and not self.disabled:
            self._auto_resume_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._auto_resume_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"run_id": self.id}))
            temporary.replace(self._auto_resume_path)
        if self.mode in {"online", "shared"}:
            self._initial_call(self.flush)
            self._background_enabled = True
            self._start_background()
        self._console = ConsoleCapture(self)
        self._console.start()
        from .notebook_capture import NotebookCapture
        self._notebook = NotebookCapture(self)
        self._notebook.start()
        from .telemetry import SystemMonitor, ModelWatcher
        self._tensorboard_seen = None
        self._tensorboard_polling = False
        tensorboard = sys.modules.get("backprop.integration.tensorboard")
        if not self.disabled and (self._settings.sync_tensorboard or
                (tensorboard is not None and tensorboard.is_patched())):
            from .init_integrations import adopt_tensorboard
            adopt_tensorboard(self)
        self._watcher = ModelWatcher(self)
        self._monitor = SystemMonitor(self)
        self._frozen = True
        self._stop_checker.start()
        self._monitor.start()

    def _initial_call(self, function):
        """Bound startup work and identify actual transport deadline failures.

        Main callers: constructor history/control reads and first publication.
        Why this exists: public init translates a real Foundry deadline into
        CommError; direct Run and local sink extensions retain TimeoutError.
        The marker changes no worker, journal acknowledgement or error text.
        """
        try:
            return bounded_call(function, max(0.000001, self._init_deadline-time.monotonic()), "init")
        except TimeoutError as error:
            from .foundry import FoundryStore
            if isinstance(self._sink, FoundryStore):
                error._backprop_init_transport_timeout = True
            raise

    def _copy_branch_files(self, value, source_dir):
        """Copy local queued references when a fork gets its own journal root."""
        from .journal_files import copy_references
        return copy_references(value, source_dir, self._journal)

    def _inherit_config_artifacts(self, events, source_dir):
        """Make inherited same-project config artifacts independent of a parent.

        Main callers: fork initialization before its run_start. Immutable IDs
        and digests stay unchanged, while local blobs and manifest records move
        into the child's recoverable journal. External-project config artifacts
        retain their explicitly qualified source identity.
        """
        ids = {value._id for value in self.config._data.values() if isinstance(value, Artifact)
               and value.entity == self.entity and value.project == self.project}
        from .artifact_events import iter_artifact_events
        manifests = {event["payload"].get("artifact_id"): event for event in iter_artifact_events(events)
                     if event["payload"].get("artifact_id") in ids}
        if not manifests:
            return
        for event in manifests.values():
            self._emit("artifact", self._copy_branch_files(event["payload"], source_dir))
        from .config_artifacts import hydrate_config
        # Re-read exactly the newly persisted descriptors. This binds Artifact
        # file access to the child even if the parent directory is later removed.
        references = {key: value._reference_json() if isinstance(value, Artifact) else value
                      for key, value in self.config._data.items()}
        original = self.config._data
        object.__setattr__(self.config, "_data", references)
        try:
            hydrated = hydrate_config(self, read_journal(self.sync_dir), self.sync_dir)
        except BaseException:
            object.__setattr__(self.config, "_data", original)
            raise
        object.__setattr__(self.config, "_data", hydrated)

    def __setattr__(self, name, value):
        if not name.startswith("_") and getattr(self, "_frozen", False) and not hasattr(self, name):
            raise Error(f"Attribute {name} is not supported on Run object")
        object.__setattr__(self, name, value)

    settings = property(lambda self: self._settings.model_copy(deep=True))
    config = property(lambda self: self._config)
    config_static = property(lambda self: ConfigStatic(self.config))
    # Keep public settings spelling (run/dryrun) while transport uses the
    # effective online/offline mode. Main callers: journal and worker lifecycle.
    mode = property(lambda self: {"run": "online", "dryrun": "offline"}.get(self._settings.mode, self._settings.mode))
    disabled = property(lambda self: self.mode == "disabled")
    offline = property(lambda self: self.mode == "offline")
    id = property(lambda self: self._settings.run_id)
    entity = property(lambda self: self._settings.entity)
    project = property(lambda self: self._settings.project)
    group = property(lambda self: self._settings.run_group or "")
    job_type = property(lambda self: self._settings.run_job_type or "")
    dir = property(lambda self: self._files_dir)
    sync_dir = property(lambda self: self._sync_dir)
    resumed = property(lambda self: self._resumed)
    step = property(lambda self: 0 if self.disabled else self._step)
    starting_step = property(lambda self: self._starting_step)
    start_time = property(lambda self: self._started)
    path = property(lambda self: f"{self.entity}/{self.project}/{self.id}")
    # Sweep URL access is installed as an explicit unsupported guard.

    @property
    def url(self):
        return self._settings.run_url

    @property
    def project_url(self):
        return self._settings.project_url

    def _metadata(self):
        return {"name": self.name, "notes": self.notes, "tags": list(self.tags), "group": self.group, "job_type": self.job_type}

    def _set_metadata(self, name, setting, value):
        self._check()
        candidate = self._settings.model_copy(update={setting: value})
        encoded = list(candidate.run_tags or ()) if name == "tags" else getattr(candidate, setting)
        self._emit("run_metadata", {"values": {name: encoded}})
        setattr(self._settings, setting, getattr(candidate, setting))

    name = property(lambda self: self._settings.run_name, lambda self, value: self._set_metadata("name", "run_name", value))
    notes = property(lambda self: self._settings.run_notes, lambda self, value: self._set_metadata("notes", "run_notes", value))

    @property
    def tags(self):
        return tuple(self._settings.run_tags or ())

    @tags.setter
    def tags(self, value):
        try:
            self._set_metadata("tags", "run_tags", value)
        except UsageError as error:
            self._warn(f"Invalid tag: {error}; tags not updated")

    def _warn(self, message):
        self._warnings.append(str(message))
        if getattr(self, "_debug", None) is not None:
            self._debug.user("WARNING " + str(message))
        if not self._settings.silent and self._settings.show_warnings:
            warnings.warn(message, RuntimeWarning, stacklevel=3)

    def _restore(self, event):
        payload, kind = event["payload"], event["type"]
        if kind == "run_start":
            self.config._data.update(payload.get("config", {}))
        elif kind in {"config", "summary"}:
            target = getattr(self, kind)._data
            target.update(payload.get("values", {}))
            for key in payload.get("deleted", []):
                target.pop(key, None)
            if kind == "config" and "annotation_labels" in payload:
                from .annotation_labels import apply_annotation_update
                apply_annotation_update(target, payload["annotation_labels"])
            if kind == "summary" and not payload.get("automatic"):
                self._reducer.explicit_update(payload.get("values", {}), payload.get("deleted", []))
        elif kind == "metric_definition":
            self._definitions[payload["name"]] = payload
            self._reducer.reconfigure(payload["name"])
        elif kind == "history":
            from .presentation import record_history
            record_history(self, payload["metrics"])
            self._step = max(self._step, payload["step"] + 1)
            self._aggregate(payload["metrics"])
            if "summary" in payload:
                self.summary._data.clear()
                self.summary._data.update(payload["summary"])
        elif kind == "history_truncate":
            self._step = payload["step"] + 1
            self.summary._data.clear()
            self.summary._data.update(payload.get("summary", {}))

    def _add_singleton(self, data_type, key, value):
        """Journal one annotation layer without overwriting other writers.

        Main callers: explicit bounding-box and mask binding. Validate a local
        candidate before appending, then update memory only after durable append.
        Repeated identical bindings need no extra event. Python/browser replay
        use the same per-layer replacement rule in stable event order.
        """
        from .config import friendly
        from .annotation_labels import apply_annotation_update
        with self._lock:
            self._check()
            entry = friendly({"type": data_type, "key": key, "value": value})
            candidate = {"_wandb": self.config._data.get("_wandb", {})}
            apply_annotation_update(candidate, entry)
            if candidate["_wandb"] != self.config._data.get("_wandb", {}):
                self._emit("config", {"annotation_labels": entry})
                self.config._data["_wandb"] = candidate["_wandb"]

    def _encoder(self, context="value", table_cache=None, media_cache=None):
        from .implicit_artifacts import prepare_table
        return Encoder(self._journal.stage_file if self._journal is not None else lambda *args, **kwargs: {},
                       context=context, settings=self._settings, run=self,
                       table_cache=table_cache, media_cache=media_cache,
                       stage_media=(lambda value: self._journal.stage_live_media(value, symlink=self._settings.symlink))
                           if self._journal is not None and self._settings.allow_media_symlink else None,
                       stage_media_batch=(lambda value: self._journal.prepare_live_media(value, symlink=self._settings.symlink))
                           if self._journal is not None and self._settings.allow_media_symlink else None,
                       artifact_factory=(lambda key, table, descriptor: prepare_table(self, key, table, descriptor)) if not self.disabled else None)

    def _prepare_config_artifacts(self, values):
        if self.disabled:
            return
        for value in values.values():
            if isinstance(value, Artifact):
                from .artifact_use import prepare_config_artifact
                prepare_config_artifact(self, value)

    def _resolve_config_artifact(self, value):
        """Turn a top-level W&B artifact URI/version value into a read object."""
        if self.disabled:
            return value
        from .config_artifacts import artifact_locator
        locator = artifact_locator(value, self._deployment.get("host"))
        if locator is None:
            return value
        if self._sink is None:
            raise Error("Config artifact URI lookup requires Foundry access; initialize online or pass a local Artifact object")
        from .apis.public import Api
        api = Api._from_store(self._sink, entity=self.entity, project=self.project, config=self._deployment)
        def lookup():
            if "/" in locator:
                return api.artifact(locator)
            from .artifact_events import iter_artifact_events
            manifests = [event for event in iter_artifact_events(self._sink.read_events())
                         if event["payload"].get("artifact_id") == locator]
            if not manifests:
                raise ValueError(f"Config artifact ID not found: {locator}")
            event = max(manifests, key=lambda item: (item["timestamp"], item["event_id"]))
            return Artifact._from_manifest(event["payload"], self._sink, event.get("entity", ""), event["project"], event["run_id"])
        return lookup() if self._frozen else self._initial_call(lookup)

    def _encode_history_value(self, key, value, table_cache=None, media_cache=None):
        """Resume first-use incremental tables transactionally, as pinned W&B does.

        Main callers: log row validation. Only a resumed run and an unlogged
        incremental Table can inherit the previous summary chain. Identity,
        schema and base offset commit together with the row; invalid input leaves
        the caller's Table untouched. Fresh non-resumed Tables retain new IDs.
        """
        encoder = self._encoder(context="history", table_cache=table_cache, media_cache=media_cache)
        encoder.key = key
        from .implicit_artifacts import PreparedCommits
        previous = self.summary._data.get(key)
        eligible = (self.resumed and isinstance(value, Table) and value.log_mode == "INCREMENTAL"
                    and value._snapshot is None and not getattr(value, "_resume_bound", False)
                    and isinstance(previous, dict) and previous.get("_type") == "table"
                    and previous.get("log_mode") == "INCREMENTAL" and not previous.get("legacy_offline"))
        if not eligible:
            encoded = encoder.encode(value)
            return encoded, PreparedCommits(encoder.commits, encoder.artifacts, encoder.prepared_tables, encoder.prepared_media)
        if previous.get("external"):
            if self._sink is None:
                raise Error("Resuming this table requires its stored descriptor; sync and initialize online")
            previous = json.loads(self._sink.download_file(previous))
        if previous.get("legacy_offline"):
            encoded = encoder.encode(value)
            return encoded, PreparedCommits(encoder.commits, encoder.artifacts, encoder.prepared_tables, encoder.prepared_media)
        from .implicit_artifacts import prepare_table
        encoder.artifact_factory = lambda name, table, descriptor: prepare_table(
            self, name, table, descriptor, previous if name == key else None)
        if previous.get("columns") != value.columns:
            raise ValueError("Resumed incremental Table columns must match the previous increment")
        from .implicit_artifacts import restore_table_types
        types = restore_table_types(self, previous) or list(value._types)
        if len(types) != len(value.columns):
            raise ValueError("Stored incremental Table schema has the wrong column count")
        for row in value.data:
            types = [dtype.assign(cell) for dtype, cell in zip(types, row)]
        identity = previous["table_id"]
        base = previous.get("row_offset", 0) + len(previous.get("data", []))
        original = value._id, value._base_offset, value._types
        value._id, value._base_offset, value._types = identity, base, types
        try:
            encoded = encoder.encode(value)
        finally:
            value._id, value._base_offset, value._types = original
        def bind():
            value._id, value._base_offset, value._types = identity, base, types
            value._resume_bound = True
        for prepared in encoder.prepared_tables.values():
            if prepared[0] is encoded:
                prepared[1].insert(0, bind)
        return encoded, PreparedCommits([bind, *encoder.commits], encoder.artifacts, encoder.prepared_tables, encoder.prepared_media)

    def _write_mapping(self, kind, values, deleted=()):
        self._check()
        encoder = self._encoder()
        if kind == "summary" and not self.disabled:
            from .summary import prepare_summary
            values = prepare_summary(values, self._journal.stage_file)
        elif kind == "config":
            self._prepare_config_artifacts(values)
        encoded = values if self.disabled else encoder.encode(values)
        manifests = list({item["artifact_id"]: item for item in encoder.artifacts}.values())
        self._emit(kind, {"values": encoded, **({"deleted": list(deleted)} if deleted else {}),
                          **({"artifacts": manifests} if manifests else {})})
        encoder.commit()
        if kind == "summary":
            self._reducer.explicit_update(encoded, deleted)
        return encoded

    def _check(self):
        if self._closed and not self.disabled:
            raise BackpropError("Run has finished")
        if self._error is not None and self._settings.stop_on_fatal_error and not retryable_failure(self._error):
            raise BackpropError(f"Background upload failed; call run.flush() to retry: {self._error}") from self._error

    def _emit(self, kind, payload):
        with self._lock:
            self._check()
            if self.disabled:
                return
            from datetime import timedelta
            self._last_event_time = max(datetime.now(timezone.utc), self._last_event_time + timedelta(microseconds=1))
            if self._shared:
                payload = {**payload, "_writer": {"id": self._writer_id,
                    "label": self._settings.x_label or self._writer_id,
                    "primary": self._settings.x_primary, "sequence": self._writer_sequence}}
                self._writer_sequence += 1
            self._journal.append({"schema_version": 1, "event_id": uuid.uuid4().hex,
                                  "timestamp": self._last_event_time.isoformat(),
                                  "run_id": self.id, "project": self.project, "entity": self.entity,
                                  "type": kind, "payload": payload})
            if self._debug is not None:
                self._debug.internal(f"Appended {kind}; journal events={self._journal.count}")

    def _background(self):
        interval = min(self._settings.flush_interval, self._settings.heartbeat_seconds)
        delay, failures = interval, 0
        while True:
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                now = time.monotonic()
                if now-self._last_heartbeat >= self._settings.heartbeat_seconds:
                    self._emit("run_metadata", {"values": {"heartbeat_at": datetime.now(timezone.utc).isoformat(),
                                                          "runtime": time.time()-self._started}})
                    self._last_heartbeat = now
                self.flush()
                delay, failures = interval, 0
            except Exception as error:
                self._error = error
                report_background_warning(self, f"Backprop upload failed; local data retained: {error}")
                if not retryable_failure(error):
                    if self._settings.stop_on_fatal_error:
                        self._stop_checker.trigger()
                    # Like W&B, permanent upload failures stop background
                    # sending while ordinary logging can still persist locally.
                    # Explicit flush/finish can retry after the cause is fixed.
                    return
                failures += 1
                delay = min(interval * (2 ** min(failures, 6)), 60)

    def _start_background(self):
        fatal = self._error is not None and not retryable_failure(self._error)
        if self._background_enabled and not fatal and not self._closed and not self._stop.is_set() and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._background, name=f"backprop-{self.id}", daemon=True)
            self._thread.start()

    def _poll_tensorboard(self, *, final=False):
        if not self._tensorboard_registered or getattr(self, "_tensorboard_polling", True) or self._closed:
            return
        self._tensorboard_polling = True
        try:
            from .integration import tensorboard
            for directory in tensorboard.directories(self): tensorboard.sync(self, directory, final=final)
        finally: self._tensorboard_polling = False

    def _release_tensorboard(self):
        """Release automatic writer hooks owned by this run exactly once.

        Main callers: failed initialization and finish. Other syncing runs and
        manually installed patches retain their independent ownership.
        """
        if self._tensorboard_registered:
            from .integration import tensorboard
            tensorboard.release_run(self)
            self._tensorboard_registered = False

    def flush(self):
        """Retry durable uploads without committing a caller's pending history row."""
        self._error = None
        self._poll_tensorboard()
        console = getattr(self, "_console", None)
        if console and not self._closed:
            console.poll()
        with self._lock:
            if self._journal is not None:
                if not self._closed:
                    self._files.poll()
        if self._journal is not None:
            if self._debug is not None:
                self._debug.internal(f"Flush started; pending={self._journal.count-self._journal.ack}")
            try:
                self._journal.flush()
            except Exception as error:
                self._error = error
                if self._debug is not None:
                    self._debug.internal(f"Flush failed: {type(error).__name__}; pending={self._journal.count-self._journal.ack}")
                raise
            if self._debug is not None:
                self._debug.internal(f"Flush completed; acknowledged={self._journal.ack}")
        with self._lock:
            self._sync_time = datetime.now()
        self._start_background()

    def _definition(self, key):
        return self._reducer.definition(key)

    def watch(self, models, criterion=None, log="gradients", log_freq=1000, idx=None, log_graph=False):
        if self.disabled:
            return None
        self._check()
        self._watcher.watch(models, criterion, log, log_freq, idx, log_graph)

    def unwatch(self, models=None):
        if self.disabled:
            return None
        self._watcher.unwatch(models)

    def define_metric(self, name, step_metric=None, step_sync=None, hidden=None, summary=None, overwrite=None):
        if self.disabled:
            return Metric("dummy")
        definition, metric = self._reducer.prepare_definition(name, step_metric, step_sync, hidden, summary, overwrite)
        self._emit("metric_definition", definition)
        self._definitions[name] = definition
        self._reducer.reconfigure(name)
        return metric

    def log(self, data, step=None, commit=None):
        """Validate all input before changing pending history or table cursors.

        Used by training loops and logging observers. Each incoming
        key owns its deferred encoder callbacks so overwritten pending values do
        not advance table cursors that never reached the journal.
        """
        with self._lock:
            if self._closed:
                raise UsageError(f"Run ({self.id}) is finished. The call to `log` will be ignored. "
                                 "Please make sure that you are using an active run.")
            self._check()
            if self.disabled:
                self.summary._data.update(data)
                return
            if not isinstance(data, Mapping):
                raise TypeError("wandb.log must be passed a dictionary")
            if any(not isinstance(key, str) for key in data):
                raise TypeError("Key values passed to wandb.log must be strings")
            if step is not None and not isinstance(step, numbers.Integral):
                raise TypeError("step must be an integer")
            if step is not None and step < self._step:
                self._warn(f"Step {step} is older than current step {self._step}; data ignored")
                return
            encoded, callbacks = {}, {}
            prepared_tables = {identity: prepared for pending in self._pending_commits.values()
                               for identity, prepared in getattr(pending, "tables", {}).items()}
            prepared_media = {}
            for pending_key, pending in self._pending_commits.items():
                if pending_key in data and not (step is not None and step > self._step):
                    continue
                for identity, prepared in getattr(pending, "media", {}).items():
                    if identity not in prepared_media or prepared[2] > prepared_media[identity][2]:
                        prepared_media[identity] = prepared
            for key, value in data.items():
                encoded[key], callbacks[key] = self._encode_history_value(key, value, prepared_tables, prepared_media)
            from .legacy_charts import chart_bindings
            for key, value in chart_bindings(data):
                value.set_key(key)
            if step is not None and step > self._step:
                if self._pending:
                    self._commit()
                self._step = int(step)
            self._pending.update(encoded)
            self._pending_commits.update(callbacks)
            if commit if commit is not None else step is None:
                self._commit()

    def _aggregate(self, metrics):
        self._reducer.apply(metrics, self.summary._data)

    def _commit(self):
        values = self._reducer.synchronize(self._pending)
        if '_tensorboard_group' in self._pending:
            from .tensorboard_steps import step_sync_annotations
            annotations = step_sync_annotations(self._pending, values)
            if annotations: values['_tensorboard_step_sync'] = annotations
        reducer = copy.deepcopy(self._reducer)
        summary = copy.deepcopy(self.summary._data)
        reducer.apply(values, summary)
        grouped = self._tensorboard_replay_active or '_tensorboard_group' in values
        if grouped:
            from datetime import timedelta
            from .tensorboard_summary import run_correction, apply_group_summary
            candidate = {'event_id': uuid.uuid4().hex, 'type': 'history',
                'timestamp': (self._last_event_time + timedelta(microseconds=1)).isoformat(),
                'entity': self.entity, 'project': self.project, 'run_id': self.id,
                'payload': {'step': self._step, 'metrics': values}}
            correction = run_correction(self, [*read_journal(self._sync_dir), candidate])
            apply_group_summary(summary, correction)
            reducer = correction['reducer']
        summary_encoder = self._encoder()
        encoded_summary = summary_encoder.encode(summary)
        manifests = [manifest for callbacks in self._pending_commits.values()
                     for manifest in getattr(callbacks, "artifacts", [])]
        manifests.extend(summary_encoder.artifacts)
        manifests = list({manifest["artifact_id"]: manifest for manifest in manifests}.values())
        self._emit("history", {"step": self._step, "metrics": values, "summary": encoded_summary,
                               **({"artifacts": manifests} if manifests else {})})
        from .presentation import record_history
        record_history(self, values)
        committed = set()
        for commits in self._pending_commits.values():
            for commit in commits:
                if id(commit) not in committed:
                    commit()
                    committed.add(id(commit))
        summary_encoder.commit()
        self._reducer = reducer
        self._tensorboard_replay_active = grouped
        self._definitions, self._last, self._stats = reducer.definitions, reducer.last, reducer.stats
        self.summary._data.clear()
        self.summary._data.update(summary)
        self._pending = {}
        self._pending_commits = {}
        self._step += 1

    @run_method(disabled_result=Noop)
    def upsert_artifact(self, artifact_or_path, name=None, type=None, aliases=None, distributed_id=None):
        from .distributed_artifacts import contribution
        return contribution(self, artifact_or_path, name, type, aliases, distributed_id)

    @run_method(disabled_result=Noop)
    def finish_artifact(self, artifact_or_path, name=None, type=None, aliases=None, distributed_id=None):
        from .distributed_artifacts import finish
        return finish(self, artifact_or_path, name, type, aliases, distributed_id)

    @run_method(disabled_result=Noop)
    def log_artifact(self, artifact_or_path, name=None, type=None, aliases=None, tags=None):
        from .artifact_logging import log_artifact
        return log_artifact(self, artifact_or_path, name, type, aliases, tags)

    @run_method
    def use_artifact(self, artifact_or_name, type=None, aliases=None, use_as=None):
        from .artifact_use import use_artifact
        return use_artifact(self, artifact_or_name, type, aliases, use_as)

    @run_method
    def log_model(self, path, name=None, aliases=None):
        from .artifact_logging import log_artifact
        log_artifact(self, path, name=name, type="model", aliases=aliases)

    @run_method
    def use_model(self, name):
        """Record a model input and return its downloaded file or directory.

        Main callers: training code and the package-level use_model function.
        The artifact lookup records the input relationship before validating the
        model type. A sole directory is unwrapped just like a sole file, matching
        the pinned helper's observable return path.
        """
        if self._settings._offline:
            raise RuntimeError("`use_model` not supported in offline mode.")
        artifact = self.use_artifact(artifact_or_name=name)
        if "model" not in str(artifact.type.lower()):
            raise AssertionError(
                "You can only use this method for 'model' artifacts."
                " For an artifact to be a 'model' artifact, its type property"
                " must contain the substring 'model'."
            )
        directory = artifact.download()
        entries = os.listdir(directory)
        return os.path.join(directory, entries[0]) if len(entries) == 1 else directory

    def log_code(self, root=".", name=None, include_fn=None, exclude_fn=None):
        """Snapshot selected source files as an immutable code Artifact.

        Main callers: explicit run.log_code and init(save_code=True). Selection
        callbacks accept either path or path/root, as in the official SDK.
        """
        if self.disabled:
            return None
        import inspect
        import re
        import fnmatch
        directory = Path(root).resolve() if root is not None else None
        def selected(callback, path, default):
            if callback is None:
                return default
            parameters = inspect.signature(callback).parameters
            return callback(str(path), str(directory)) if len(parameters) > 1 else callback(str(path))
        generated_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", f"source-{self.project}-{self._settings.program_relpath}")
        artifact = Artifact._new_internal(name or generated_name, type="code")
        count = 0
        for path in sorted(directory.rglob("*")) if directory else []:
            if not path.is_file():
                continue
            relative = path.relative_to(directory)
            ignored = any(fnmatch.fnmatchcase(relative.as_posix(), pattern) for pattern in self._settings.ignore_globs)
            excluded = any(part in {".git", "wandb", ".wandb", ".backprop"} for part in relative.parts)
            default_include = path.suffix == ".py"
            if ignored or selected(exclude_fn, path, excluded) or not selected(include_fn, path, default_include):
                continue
            artifact.add_file(path, name=relative.as_posix())
            count += 1
        staged = Path(self.sync_dir) / "tmp/code"
        if staged.is_dir():
            for path in sorted(staged.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(staged).as_posix()
                    if any(fnmatch.fnmatchcase(relative, pattern) for pattern in self._settings.ignore_globs):
                        continue
                    artifact.add_file(path, name=relative)
                    count += 1
        if not count:
            self._warn("No relevant files were detected; no code was logged")
            return None
        result = self.log_artifact(artifact)
        self.config.update({"_wandb": {**self.config.get("_wandb", {}), "code_path": artifact.name}}, allow_val_change=True)
        return result

    def save(self, glob_str, base_path=None, policy="live", glob=True):
        if self.disabled:
            return False
        self._check()
        with self._lock:
            return self._files.save(glob_str, base_path, policy, glob)

    def _publish_file(self, name):
        """Publish an explicitly bound media file through ordinary run files.

        Main callers: Media.bind_to_run. This preserves file naming and recovery
        while sharing the same durable journal as save(), with no extra uploader.
        """
        return self.save(str(Path(self.dir) / name), base_path=self.dir, policy="now", glob=False)

    def restore(self, name, run_path=None, replace=False, root=None):
        if self.disabled:
            return None
        target = run_path or self.path
        entity, project, run_id = target.rsplit("/", 2)
        if self._sink is None:
            raise Error("Restore needs an authenticated store")
        events = self._sink.read_events(project=project, run_id=run_id, entity=entity)
        return restore_file(self._sink, events, name, replace=replace, root=root or self.dir)

    def status(self):
        if self.disabled:
            return None
        self._check()
        return RunStatus(self._journal.count, self._journal.count-self._journal.ack, self._sync_time) if self._journal else RunStatus()

    def write_logs(self, text):
        if self._closed or self.disabled:
            return
        if not isinstance(text, str):
            raise TypeError("write_logs expects text")
        self.log_console(text if text.endswith("\n") else text + "\n")

    def log_console(self, text, stream="stdout"):
        if self._closed or self.disabled:
            return
        if stream not in {"stdout", "stderr"}:
            raise ValueError("Console stream must be stdout or stderr")
        console = getattr(self, "_console", None)
        if console:
            console.write(str(text), stream)
        else:
            self._emit("console", {"text": str(text), "stream": stream})

    def pin_config_keys(self, keys=()):
        values = list(keys)
        self.config.update({"_wandb": {**self.config.get("_wandb", {}), "pinned_keys": values}}, allow_val_change=True)
        self._emit("run_metadata", {"values": {"pinned_config_keys": values}})

    def mark_preempting(self):
        """Queue the preemption marker and request immediate background sending.

        Main callers: training code about to yield or checkpoint. Like the
        pinned SDK this call signals the uploader without blocking on network;
        offline/disabled runs do not create an uploader or perform network I/O.
        """
        self._emit("run_metadata", {"values": {"preempting": True}})
        if not self.disabled and not self.offline:
            self._wake.set()

    def get_url(self):
        return self.url

    def to_html(self, height=420, hidden=False):
        import html
        from .settings import in_vscode_notebook
        if in_vscode_notebook():
            return html.escape(str(self))
        if not self.url:
            return None
        frame = f'<iframe src="{html.escape(self.url, quote=True)}" height="{int(height)}" style="width:100%;border:0;"></iframe>'
        return '<details><summary>Show Backprop run</summary>' + frame + '</details>' if hidden else frame

    def display(self, height=420, hidden=False):
        from .settings import in_jupyter, in_vscode_notebook
        if self._settings.silent or not in_jupyter() or in_vscode_notebook():
            return False
        try:
            from IPython.display import HTML, display
        except ImportError:
            return False
        markup = self.to_html(height, hidden)
        if markup is None:
            return False
        display(HTML(markup))
        return True

    def finish(self, exit_code=None, quiet=None):
        """Close pending work and reset public globals; failures retain the journal."""
        if self._closed:
            return
        deadline = time.monotonic() + self._settings.finish_timeout if self._settings.finish_timeout else None
        import_error = None
        try:
            self._poll_tensorboard(final=True)
        except BaseException as error:
            # An unsupported or malformed source must fail visibly, but cannot
            # keep capture hooks and monitor threads alive after finish returns.
            import_error = error
        finally:
            self._release_tensorboard()
        self._monitor.stop()
        self._watcher.unwatch()
        self._stop.set()
        self._wake.set()
        if self._stop_checker is not None:
            self._stop_checker.stop_soon()
        self._error = None
        if self._notebook is not None:
            self._notebook.finish(deadline=deadline)
        self._console.stop()
        with self._lock:
            if self._pending:
                self._commit()
            if self._journal:
                self._files.poll(ending=True)
            encoder = self._encoder()
            encoded = encoder.encode(self.summary._data) if not self.disabled else {}
            self._emit("summary", {"values": encoded, "automatic": True,
                                   **({"artifacts": encoder.artifacts} if encoder.artifacts else {})})
            encoder.commit()
            code = self._unhandled_exit_code if exit_code is None else int(exit_code)
            if import_error is not None and code == 0:
                code = 1
            self._emit("run_finish" if not self._shared or self._settings.x_primary and self._settings.x_update_finish_state else "writer_finish",
                       {"exit_code": code, "state": "finished" if code == 0 else "failed"})
            self._closed = True
        try:
            remaining = max(0.000001, deadline-time.monotonic()) if deadline else None
            bounded_call(lambda: self._journal.flush(deadline=deadline) if self._journal else None, remaining, "finish")
            self._sync_time = datetime.now()
        except TimeoutError as error:
            self._warn(str(error))
            if self._settings.finish_timeout_raises:
                raise
        finally:
            from .presentation import print_footer
            try:
                print_footer(self, quiet=quiet)
            finally:
                try:
                    if self._debug is not None:
                        self._debug.user(f"Run finished; exit_code={code}")
                        self._debug.close()
                finally:
                    module = sys.modules.get("backprop")
                    if module and hasattr(module, "_on_finish"):
                        module._on_finish(self)
        if code == 0 and self._auto_resume_path.is_file():
            if json.loads(self._auto_resume_path.read_text()).get("run_id") == self.id:
                self._auto_resume_path.unlink()
        if import_error is not None:
            raise import_error

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.finish(exit_code=0 if kind is None else 1)
