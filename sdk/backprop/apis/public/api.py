"""Editable experiment API using Foundry's stream/archive and file dataset.

Architecture: resolve project/run paths -> scoped event reads -> event-backed Python
views. Cached single-run snapshots refresh through load(force=True) or Api.flush.
No W&B service, protocol emulation, proxy, database or mutable index is involved.
Main callers: analysis notebooks and logging Run.restore/use_artifact.
"""
from __future__ import annotations

from backprop.errors import UnsupportedFeatureError

from collections import defaultdict
import copy
import json
import math
import os
from urllib.parse import parse_qs, urlsplit

from ...foundry import FoundryError, FoundryStore
from ...errors import CommError
from ...artifact_events import iter_artifact_events
from ...artifact_updates import artifact_views
from ...transport import load_config
from .artifacts import (ArtifactCollection, ArtifactCollections, Artifacts,
                        ArtifactType, ArtifactTypes, ProjectArtifactCollections)
from .projects import Project, Projects
from ..normalize import normalize_exceptions
from .query import matches, ordered
from .runs import Run, RunNotFoundError, Runs


def _is_foundry_transport_error(error):
    """Map the pinned existence-check transport distinction to Foundry errors.

    Main callers: artifact_exists and artifact_collection_exists. These public
    methods intentionally return False for normalized validation and response
    failures, but must propagate a missing response or an HTTP 408/504 timeout.
    FoundryError retains the response status without parsing sensitive messages.
    Other Error subclasses keep the pinned normalizer's existing behavior.
    """
    cause = error if isinstance(error, FoundryError) else error.exc
    return isinstance(cause, FoundryError) and cause.status_code in {None, 408, 504}


class Api:
    """Query experiment data with a Foundry token and Backprop deployment config."""

    _HTTP_TIMEOUT = int(os.environ.get("WANDB_HTTP_TIMEOUT", 19))

    def __init__(self, overrides=None, timeout=None, api_key=None):
        """Resolve public defaults, verify credentials, then expose the reader.

        Main callers: notebooks and the W&B-compatible public Api constructor.
        Why this exists: inspection must reject a bad token at construction,
        rather than defer the failure to an unrelated later query. Verification
        uses the configured Foundry stream and never publishes experiment data.
        Overrides are retained as public settings, matching the pinned API.
        Keeping an organization default does not invoke an organization service;
        excluded operations still fail when called. Only documented connection
        fields configure this Foundry reader.
        """
        from ...run import resolve_settings
        from ...login_session import (authenticate_candidate, current_init_session,
                                     persist_candidate, remember_init_session)
        from ...errors import AuthenticationError, UsageError
        from ...runtime import bounded_call
        overrides = dict(overrides or {})
        selected = resolve_settings()
        self._config = dict(load_config())
        host = (overrides.get("base_url") if "base_url" in overrides else
                os.environ.get("BACKPROP_HOST") or os.environ.get("WANDB_BASE_URL") or
                (selected.base_url if "base_url" in selected._explicit else self._config.get("host")))
        self._config["host"] = host
        self._timeout = self._HTTP_TIMEOUT if timeout is None else timeout
        if not isinstance(self._timeout, (int, float)) or not math.isfinite(self._timeout):
            raise ValueError("timeout must be a finite number")
        self._config["timeout"] = self._timeout
        self.settings = {"base_url": host,
                         "entity": os.environ.get("WANDB_ENTITY", selected.entity),
                         "project": os.environ.get("WANDB_PROJECT", selected.project),
                         "organization": None, **overrides}
        proxies = overrides.get("_proxies") or json.loads(os.environ.get("WANDB__PROXIES", "{}"))
        for field in ("http_proxy", "https_proxy", "insecure_disable_ssl"):
            if field in selected._explicit:
                self._config[field] = getattr(selected, field)
        # Like the pinned constructor, retain the complete override while only
        # reading the two transport keys it actually consumes. Extra entries
        # remain observable in Api.settings and do not select another protocol.
        connection_settings = selected.model_copy(deep=True)
        for scheme in ("http", "https"):
            value = proxies.get(scheme)
            if value:
                setattr(connection_settings, scheme + "_proxy", value)
                self._config[scheme + "_proxy"] = getattr(connection_settings, scheme + "_proxy")
        if not host or not self._config.get("stream_dataset_rid"):
            raise ValueError("Api requires BACKPROP_CONFIG or BACKPROP_HOST and BACKPROP_STREAM_DATASET_RID")
        session = current_init_session(host)
        # Public inspection reuses configured credentials even when init has
        # relogin=True. Empty explicit keys select that same implicit path.
        # Foundry has one usable inspection choice: enter an existing token.
        # Deadline/EOF cancel construction without selecting a logging mode.
        try:
            candidate = authenticate_candidate(key=api_key or None, host=host, prompt=True,
                                               verify=False, timeout=None, settings=selected,
                                               relogin=False, force=True, raise_prompt_timeout=True,
                                               load_config=lambda: self._config, native_store=FoundryStore,
                                               session_key=session.token if session else None,
                                               session_host=session.host if session else None)
        except EOFError:
            raise KeyboardInterrupt() from None
        if candidate.mode != "online":
            raise UsageError("Api inspection requires online Foundry credentials")
        self._config["host"] = candidate.host or host
        self.settings["base_url"] = self._config["host"]
        self.api_key = candidate.token
        self._store = FoundryStore(self._config, token=self.api_key)
        try:
            # A zero/negative public timeout expires authentication; it does
            # not mean the unbounded wait used by some logging operations.
            if self._timeout <= 0:
                raise TimeoutError("Api authentication deadline has already expired")
            bounded_call(self._store.verify_credentials, self._timeout, "Api authentication")
        except Exception as error:
            raise AuthenticationError("Foundry credentials could not be verified for Api inspection") from error
        if candidate.source == "prompt":
            persist_candidate(candidate, selected)
        if not api_key:
            from ... import _publish_login_defaults, _setup_settings
            # Only authentication belongs to the process. Reader resolution
            # may include newer environment defaults or local proxy overrides.
            defaults = (_setup_settings if _setup_settings is not None else selected).model_copy(deep=True)
            defaults.api_key = candidate.token
            defaults.base_url = candidate.host
            _publish_login_defaults(defaults)
            remember_init_session(candidate)
        self._cache = {}

    @classmethod
    def _from_store(cls, store, *, entity="", project="uncategorized", config=None):
        """Construct a deterministic local reader for tests and SDK retrieval.

        Main callers: the logging Run's file/artifact helpers. It reuses the
        already authenticated store, avoiding another credential configuration.
        """
        api = cls.__new__(cls)
        api._store, api._config, api.api_key = store, dict(config or getattr(store, "config", {})), getattr(store, "token", None)
        api.settings, api._cache = {"entity": entity, "project": project, "run": ""}, {}
        return api

    @property
    def default_entity(self): return self.settings.get("entity")
    @property
    def user_agent(self): return "Backprop Foundry Public Api"
    @property
    def viewer(self): raise UnsupportedFeatureError("Foundry identity replaces W&B user/account administration")

    def flush(self):
        self._cache.clear()

    def _project_path(self, path):
        if path is None or path == "": return self.default_entity or "", self.settings.get("project") or "uncategorized"
        if not isinstance(path, str): raise ValueError("project path must be a string")
        parts = path.split("/")
        if len(parts) == 1 and parts[0]: return self.default_entity or "", parts[0]
        if len(parts) == 2 and parts[1]: return tuple(parts)
        raise ValueError("Project path must be project or entity/project")

    def _object_path(self, path, kind):
        if not isinstance(path, str): raise ValueError(f"{kind} path must be a string")
        if not path and kind == "run": path = self.settings.get("run", "")
        if kind == "run":
            path = path.replace("/runs/", "/").strip("/ ")
            if ":" in path:
                head, identifier = path.rsplit(":", 1)
                path = head + "/" + identifier
            path = "/".join(part.strip() for part in path.split("/"))
        parts = path.split("/")
        if len(parts) == 1: parts = [self.default_entity or "", self.settings.get("project") or "uncategorized", *parts]
        elif len(parts) == 2: parts = [self.default_entity or "", *parts]
        if len(parts) != 3 or not parts[1] or not parts[2]: raise ValueError(f"Expected entity/project/{kind}, project/{kind}, or {kind}")
        return tuple(parts)

    def from_path(self, path):
        if not isinstance(path, str): raise ValueError("path must be a string")
        if "://" in path:
            url = urlsplit(path)
            if urlsplit(self._config.get("app_url", "")).netloc != url.netloc:
                raise ValueError("URL belongs to another Backprop deployment")
            query = parse_qs(url.query)
            project = query.get("project", [None])[0]
            if not project: raise ValueError("Workspace URL has no project")
            return self.run(project + "/" + query["run"][0]) if "run" in query else self.project(project)
        parts = path.split("/")
        if len(parts) == 4 and parts[-2] == "sweeps":
            raise UnsupportedFeatureError("Api.from_path cannot inspect sweeps; sweeps are not implemented in Backprop")
        if len(parts) == 1 and parts[0]: return self.project(path)
        if len(parts) == 2: return self.project(path)
        if len(parts) == 4 and parts[-2] == "runs": return self.run("/".join(parts[:2] + parts[-1:]))
        if len(parts) == 3: return self.run(path)
        raise ValueError("Expected a project or run path")

    def projects(self, entity=None, per_page=200):
        scope = self.default_entity if entity is None else entity
        def fetch():
            grouped = defaultdict(list)
            for event in self._store.read_events(entity=scope or ""):
                grouped[(event.get("entity") or "", event["project"])].append(event)
            return [Project(self, owner, project, events) for (owner, project), events in sorted(grouped.items())
                    if any(event["type"] in {"run_start", "project_create"} for event in events)]
        return Projects(fetch, per_page)

    def project(self, name, entity=None):
        owner, project = self._project_path(name)
        if entity is not None: owner = entity
        events = self._store.read_events(entity=owner, project=project)
        if not any(event["type"] in {"run_start", "project_create"} for event in events): raise ValueError(f"Project not found: {owner}/{project}")
        return Project(self, owner, project, events)

    def sync_tensorboard(self, root_dir, run_id=None, project=None, entity=None):
        """Import source events and return the experiment's public read handle.

        Main callers: experiment inspection clients importing existing logs.
        The acknowledged journal seeds the first snapshot, just like create_run,
        so Foundry's delayed query visibility cannot hide a successful import.
        Later refreshes use the ordinary Foundry reader and its visibility rules.
        """
        import uuid
        from ...run import Run as LoggingRun
        from ...integration.tensorboard import sync
        from ...transport import read_journal
        run_id=run_id or uuid.uuid4().hex[:8]
        project=project or self.settings.get("project") or "uncategorized"
        entity=entity or self.default_entity or ""
        with LoggingRun(id=run_id, project=project, entity=entity, mode="online",
                        resume="allow", _sink=self._store, _deployment=self._config,
                        settings={"console":"off","silent":True,"disable_git":True,"x_disable_stats":True,
                                  "sync_tensorboard":False}) as run:
            run._tensorboard_historical_import=True
            run.define_metric("global_step")
            run.define_metric("*", step_metric="global_step", step_sync=True)
            sync(run, root_dir)
        events=[run._journal._resolve(event) for event in read_journal(run.sync_dir)]
        result=Run._from_events(self,entity,project,run_id,events)
        self._cache[("run",entity,project,run_id)]=result
        return result

    def create_custom_chart(self, entity, name, display_name, spec_type, access, spec):
        from ...custom_charts import register
        return register(self, entity, name, display_name, spec_type, access, spec)

    def create_project(self, name, entity):
        from .mutations import writer_for
        if not isinstance(name, str) or not name or any(c in name for c in "/\\"):
            raise ValueError("Project name must be nonempty without path separators")
        writer = writer_for(self, entity or "", name, "__project__")
        writer.write("project_create", {"name": name})
        self.flush()
        return None

    def create_run(self, *, run_id=None, project=None, entity=None):
        return self._create_run(run_id=run_id, project=project, entity=entity)

    def _create_run(self, *, run_id=None, project=None, entity=None, state="running"):
        from .mutations import writer_for
        import uuid
        entity = self.default_entity or "" if entity is None else entity
        project = project or self.settings.get("project") or "uncategorized"
        run_id = run_id or uuid.uuid4().hex[:8]
        if not isinstance(run_id, str) or any(c in run_id for c in "/\\"):
            raise ValueError("Invalid run ID")
        if state not in {"running", "pending"}: raise ValueError("Initial state must be running or pending")
        existing = self._store.read_events(entity=entity, project=project, run_id=run_id)
        if any(e["type"] == "run_start" for e in existing):
            return Run._from_events(self, entity, project, run_id, existing)
        writer = writer_for(self, entity, project, run_id)
        writer.write("run_start", {"name": run_id, "config": {}, "state": state})
        self.flush()
        return Run._from_events(self, entity, project, run_id, writer.published_events())

    def runs(self, path=None, filters=None, order="+created_at", per_page=50, include_sweeps=False, lazy=True):
        if include_sweeps: raise UnsupportedFeatureError("Sweeps are outside the experiment logging support boundary")
        entity, project = self._project_path(path)
        matches({}, filters)
        ordered([], order)
        cache_key = ("runs", entity, project, json.dumps(filters or {}, sort_keys=True), order, per_page)
        if cache_key not in self._cache:
            def fetch():
                grouped = defaultdict(list)
                for event in self._store.read_events(entity=entity, project=project): grouped[event["run_id"]].append(event)
                runs = [Run._from_events(self, entity, project, run_id, events, lazy=collection._lazy)
                        for run_id, events in grouped.items()
                        if any(event["type"] == "run_start" for event in events)]
                return ordered([run for run in runs if matches(run._record(), filters)], order, record=lambda run: run._record())
            collection = Runs(fetch, per_page, lazy=lazy)
            self._cache[cache_key] = collection
        result = self._cache[cache_key]
        if not lazy and result._lazy: result.upgrade_to_full()
        return result

    @normalize_exceptions
    def run(self, path=""):
        entity, project, run_id = self._object_path(path, "run")
        key = ("run", entity, project, run_id)
        if key not in self._cache: self._cache[key] = Run(self, entity, project, run_id, lazy=False)
        return self._cache[key]

    def _artifact_events(self, entity=None, project=None):
        return [{**event, "payload": {"created_at": event["timestamp"], **event["payload"]}}
                for event in artifact_views(self._store.read_events(entity=entity, project=project))]

    def _make_artifact(self, payload, entity, project, run_id):
        from ...artifacts import Artifact
        return Artifact._from_manifest(copy.deepcopy(payload), self._store, entity, project, run_id)

    @normalize_exceptions
    def artifact(self, name, type=None):
        from ...artifact_paths import lookup_path
        path = lookup_path(name, entity=self.settings.get("entity") or self.default_entity, project=self.settings.get("project"))
        entity, project, identity = path.prefix, path.project, path.name
        collection, separator, version = identity.partition(":")
        events = self._artifact_events(entity, project)
        if separator:
            candidates = [event for event in events if event["payload"]["name"] == collection]
            # An alias must never shadow an immutable content version. Resolve
            # that identity first even when a newer artifact claims its string.
            selected = [event for event in events if
                        identity == str(event["payload"]["name"]) + ":" + str(event["payload"].get("version"))]
            if not selected:
                selected = [event for event in candidates if version in event["payload"].get("aliases", [])]
        else:
            selected = [event for event in events if event["payload"].get("artifact_id") == identity]
        if not selected:
            if not separator and any(event["payload"]["name"] == identity for event in events):
                raise ValueError("Specify name:latest, name:alias, or the content version from artifact.version")
            raise ValueError(f"Artifact not found: {name}")
        event = selected[-1]
        result = self._make_artifact(event["payload"], entity, project, event["run_id"])
        if type is not None and result.type != type: raise ValueError(f"type {type!r} specified but this artifact is of type {result.type!r}")
        return result

    @normalize_exceptions
    def artifact_exists(self, name, type=None):
        """Return the pinned boolean result, preserving transport uncertainty.

        Normalized validation and HTTP response failures return False. With
        WANDB_DEBUG enabled, raw validation exceptions propagate instead.
        """
        try: self.artifact(name, type); return True
        except CommError as error:
            if _is_foundry_transport_error(error): raise
            return False

    @normalize_exceptions
    def artifacts(self, type_name, name, order=None, per_page=50, tags=None, start=None):
        entity, project, collection = self._object_path(name, "artifact")
        if tags is not None and (not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)):
            raise ValueError("tags must be a list of strings")
        def fetch():
            events = [event for event in self._artifact_events(entity, project)
                      if event["payload"]["name"] == collection and event["payload"]["artifact_type"] == type_name
                      and all(tag in event["payload"].get("tags", []) for tag in tags or [])]
            events = ordered(events, order or "-created_at", record=lambda event: {"createdAt": event["timestamp"], "updatedAt": event["timestamp"], **event["payload"]})
            unique = {event["payload"].get("artifact_id", event["event_id"]): event for event in events}
            return [self._make_artifact(event["payload"], entity, project, event["run_id"]) for event in unique.values()]
        return Artifacts(fetch, per_page, start=start, scope=json.dumps([entity, project, type_name, collection, order, tags]))

    @normalize_exceptions
    def artifact_versions(self, type_name, name, per_page=50): return self.artifacts(type_name, name, per_page=per_page)

    def _collections(self, entity, project, type_name=None, filters=None, order=None, per_page=50, start=None):
        matches({}, filters)
        def fetch():
            from ...artifact_collections import collection_catalog
            snapshot = self._store.read_events(entity=entity, project=project)
            records, _ = collection_catalog(snapshot)
            grouped = defaultdict(list)
            for event in artifact_views(snapshot):
                payload = event["payload"]
                if type_name is None or type_name == payload["artifact_type"]: grouped[(payload["name"], payload["artifact_type"])].append(event)
            values = [ArtifactCollection(self, entity, project, name, type, events,
                record=records[events[0]['payload']['_collection_id']]) for (name, type), events in grouped.items()]
            return ordered([value for value in values if matches(value._record(), filters)], order or "+name", record=lambda value: value._record())
        cls = ProjectArtifactCollections if type_name is None else ArtifactCollections
        return cls(fetch, per_page, start=start, scope=json.dumps([entity, project, type_name, filters, order], sort_keys=True))

    @normalize_exceptions
    def artifact_collections(self, project_name, type_name, order=None, per_page=50, start=None):
        entity, project = self._project_path(project_name)
        return self._collections(entity, project, type_name, order=order, per_page=per_page, start=start)

    @normalize_exceptions
    def artifact_collection(self, type_name, name):
        entity, project, collection = self._object_path(name, "artifact")
        values = self._collections(entity, project, type_name, filters={"name": collection})
        if not values: raise ValueError(f"Artifact collection not found: {name}")
        return values[0]

    @normalize_exceptions
    def artifact_collection_exists(self, name, type):
        """Use the same boolean/error rules as artifact_exists."""
        try: self.artifact_collection(type, name); return True
        except CommError as error:
            if _is_foundry_transport_error(error): raise
            return False

    @normalize_exceptions
    def artifact_types(self, project=None, start=None):
        entity, project = self._project_path(project)
        def fetch():
            return [ArtifactType(self, entity, project, name) for name in sorted({event["payload"]["artifact_type"] for event in self._artifact_events(entity, project)})]
        return ArtifactTypes(fetch, start=start, scope=json.dumps([entity, project, "types"]))

    @normalize_exceptions
    def artifact_type(self, type_name, project=None):
        for result in self.artifact_types(project):
            if result.name == type_name: return result
        raise ValueError(f"Artifact type not found: {type_name}")
