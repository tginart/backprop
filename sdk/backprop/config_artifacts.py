"""Restore usable immutable Artifact values in resumed run configuration.

Architecture: locate an immutable manifest in prior events -> reconstruct the
Artifact object -> lazily read verified bytes from the source journal or Foundry.
Cross-project references use their explicit provenance. No file contents are
downloaded just to resume scalar configuration.
Main callers: Run initialization after history replay and config overrides.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from .errors import Error
from .scalar_values import decode_numbers


def is_config_artifact(value):
    from .data_types import Artifact
    return (isinstance(value, Artifact)
            or isinstance(value, str) and value.startswith("wandb-artifact://")
            or isinstance(value, dict) and value.get("_type") == "artifactVersion")


def artifact_locator(value, host=None):
    """Parse official config references while keeping credentials on Foundry.

    Main callers: Run's config resolver. Explicit alternate hosts are rejected;
    a source SDK URI never authorizes sending a Foundry token to another service.
    """
    if isinstance(value, dict) and value.get("_type") == "artifactVersion":
        identity = value.get("id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Artifact version configuration requires an id")
        return identity
    if not isinstance(value, str) or not value.startswith("wandb-artifact://"):
        return None
    text = value[len("wandb-artifact://"):]
    parsed = urlsplit(text)
    if parsed.scheme:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if not host or origin.rstrip("/") != host.rstrip("/"):
            raise ValueError("Config artifact URI host must match the configured Foundry host")
        parts = parsed.path.strip("/").split("/")
    else:
        parts = text.split("/")
    if parts[0] == "_id" and len(parts) >= 2 and parts[1]:
        return parts[1]
    if len(parts) < 3 or not all(parts[:3]):
        raise ValueError("Config artifact URI needs entity/project/immutable-version or _id/artifact-id")
    return "/".join(parts[:3])


class JournalArtifactStore:
    """Provide read-only, integrity-checked access to a parent's queued files."""

    def __init__(self, directory, events, remote):
        self.directory = Path(directory).resolve()
        self.events, self.remote = events, remote

    def download_file(self, reference):
        relative = reference.get("_backprop_local_file")
        if relative is None:
            if self.remote is None:
                raise Error("This artifact file is stored in Foundry; initialize online to download it")
            return self.remote.download_file(reference)
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory):
            raise Error("Artifact file reference escapes its source journal")
        data = path.read_bytes()
        if len(data) != reference.get("size") or hashlib.sha256(data).hexdigest() != reference.get("sha256"):
            raise Error("Queued artifact file failed size or SHA-256 validation")
        return data

    def read_events(self, **filters):
        local = [event for event in self.events if all(value is None or event.get(key) == value for key, value in filters.items())]
        remote = self.remote.read_events(**filters) if self.remote is not None else []
        return sorted({event["event_id"]: event for event in [*local, *remote]}.values(), key=lambda event: (event["timestamp"], event["event_id"]))


def hydrate_config(run, events, source_dir):
    """Return an atomic config replacement with native scalars and Artifacts.

    Main callers: Run.__init__. A missing referenced manifest raises before the
    resumed run emits a start event instead of leaving a misleading plain dict.
    """
    from .data_types import Artifact
    from .apis.public import Api
    from .artifact_events import iter_artifact_events
    result = decode_numbers(run.config._data)
    manifests = list(iter_artifact_events(events))
    for key, reference in result.items():
        if not isinstance(reference, dict) or reference.get("_type") != "artifact":
            continue
        entity = reference.get("source_entity", run.entity)
        project = reference.get("source_project", run.project)
        manifest = next((event for event in reversed(manifests)
                         if event["payload"].get("artifact_id") == reference.get("artifact_id")
                         and event.get("entity", "") == entity and event.get("project") == project), None)
        if manifest is not None:
            store = JournalArtifactStore(source_dir, events, run._sink)
            artifact = Artifact._from_manifest(manifest["payload"], store, entity, project, manifest["run_id"])
            from .deferred_incremental_artifacts import restored_publication_status
            publication = restored_publication_status(artifact)
            if publication is not None:
                artifact._committed = publication and not (artifact._incremental_pending or artifact._distributed_pending)
            elif any("_backprop_local_file" in file for file in manifest["payload"].get("files", [])):
                artifact._committed = False
        elif run._sink is not None:
            api = Api._from_store(run._sink, entity=entity, project=project, config=run._deployment)
            artifact = run._initial_call(lambda: api.artifact(reference["artifact_id"]))
        else:
            raise Error(f"Config artifact {key!r} has no local manifest; sync its source and initialize online")
        if reference.get("digest") and artifact.digest != reference["digest"]:
            raise Error(f"Config artifact {key!r} failed immutable digest validation")
        result[key] = artifact
    return result
