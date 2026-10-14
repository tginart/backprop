"""Immutable artifact manifests with stored snapshots or external references.

Architecture: add_file copies bytes into an artifact-owned temporary directory;
the manifest records checksums of exactly those copies; logging stages the copies
in the run journal and appends one immutable manifest event. Read-side artifacts
reconstruct the same entries and ask the store for bytes only when downloaded.
External references instead record provider URI/checksum/version metadata, never
uploading their bytes; their reads use the original provider's credentials.
No mutable alias index, global version counter, registry, or external server is
introduced. Main callers: Run.log_artifact and the public read API.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
from types import MappingProxyType
from typing import Any
import uuid
import weakref
from urllib.parse import urlsplit, quote

from .artifact_identity import ensure_logged
from .artifact_inputs import metadata as _validate_metadata
from .artifact_checksums import bytes_digest, file_checksums as _checksums, hasher, manifest_digest


class ArtifactDigestAlgorithm(str, Enum):
    MANIFEST_MD5 = "MANIFEST_MD5"
    MANIFEST_XXH128 = "MANIFEST_XXH128"


class _FrozenDict(dict):
    def _blocked(self, *args, **kwargs):
        raise TypeError("Logged artifact metadata and manifests are immutable")
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _blocked


class _FrozenList(list):
    def _blocked(self, *args, **kwargs):
        raise TypeError("Logged artifact metadata and manifests are immutable")
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _blocked


def _freeze(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _logical_path(value):
    """Validate a portable manifest path before joining it to a local directory."""
    name = str(value).replace("\\", "/")
    path = PurePosixPath(name)
    if not name or str(path) == "." or name.endswith("/") or path.is_absolute() or ".." in path.parts or "\0" in name or re.match(r"^[A-Za-z]:", name):
        raise ValueError("Artifact paths must be nonempty relative file names")
    return str(path)


def _collection_name(value):
    """Validate identity independently of later local download path validation."""
    if not isinstance(value, str) or len(value) > 128 or "/" in value:
        raise ValueError("Artifact name must be a string of at most 128 characters without /")
    return value


@dataclass(frozen=True, init=False, repr=False)
class ArtifactManifestEntry:
    """One immutable file, shared by locally created and remotely read artifacts."""

    path: str
    digest: str
    size: int | None = None
    sha256: str | None = None
    ref: str | None = None
    birth_artifact_id: str | None = None
    extra: dict = field(default_factory=dict)
    local_path: str | None = None
    skip_cache: bool = False
    _parent: object = field(default=None, repr=False)
    _reference: dict | None = field(default=None, repr=False)
    _public_fields_set: frozenset = field(default_factory=frozenset, repr=False, compare=False)

    from .artifact_entry_fields import ENTRY_SIGNATURE as __signature__
    from .artifact_entry_serialization import public_entry_serializer as __pydantic_serializer__

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        """Serialize entry-typed fields without exposing native dataclass state.

        Main callers: Pydantic models embedding an existing manifest entry.
        """
        from .artifact_entry_serialization import public_entry_schema
        return public_entry_schema(cls)

    def __init__(self, /, **data):
        """Normalize keyword fields, then freeze native immutable entry state.

        Main callers: Artifact.add_file, manifest hydration and direct users.
        Validation happens before setting attributes, preserving a complete
        immutable object even when field coercion or local size lookup fails.
        """
        from .artifact_entry_fields import fields
        values = fields(data)
        public_names = {"path", "digest", "ref", "birth_artifact_id", "size", "extra", "local_path", "skip_cache"}
        explicit = set(data) & public_names
        if "birthArtifactID" in data:
            explicit.add("birth_artifact_id")
        if data.get("size") is None and values["local_path"]:
            explicit.add("size")
        object.__setattr__(self, "_public_fields_set", frozenset(data.get("_public_fields_set", explicit)))
        for name, value in values.items():
            object.__setattr__(self, name, _freeze(value) if name in {"extra", "_reference"} else value)

    def __repr__(self):
        """Match the pinned custom display without native storage internals."""
        values = self.model_dump(by_alias=False, exclude_none=True,
                                 exclude=None if self.extra else {"extra"})
        return f"{type(self).__qualname__}({', '.join(f'{key}={value!r}' for key, value in values.items())})"

    @property
    def name(self):
        from . import termwarn
        termwarn("ArtifactManifestEntry.name is deprecated, use .path instead.", repeat=False)
        return self.path

    def parent_artifact(self):
        if self._parent is None:
            raise NotImplementedError
        return self._parent

    def digest_algorithm(self):
        return (ArtifactDigestAlgorithm.MANIFEST_XXH128 if self.extra.get("alg") == "XXH128"
                else ArtifactDigestAlgorithm.MANIFEST_MD5)

    def to_json(self):
        """Return the pinned public dictionary, including canonical aliases."""
        return self.model_dump(exclude_none=True)

    def model_dump(self, *, mode: str = "json", **kwargs: Any) -> dict[str, Any]:
        """Return the public entry with all upstream serialization options.

        Main callers: clients exporting manifests as Python/JSON-ready mappings.
        Defaults match W&B's JsonableModel, including alias and round-trip flags.
        """
        from .artifact_entry_serialization import projection
        return projection(self).model_dump(mode=mode, **{"by_alias": True, "round_trip": True, **kwargs})

    def model_dump_json(self, *, indent: int | None = None, **kwargs: Any) -> str:
        """Return public-entry JSON with the runtime's validation and formatting.

        Main callers: clients writing standalone manifest descriptions. Native
        file references, parent objects and checksum extensions remain private.
        """
        from .artifact_entry_serialization import projection
        return projection(self).model_dump_json(indent=indent, **{"by_alias": True, "round_trip": True, **kwargs})

    def _storage_json(self):
        """Keep native manifest checksums separate from the public wrapper."""
        from .external_references import is_external, MARKER
        result = {key: value for key, value in {"path": self.path, "digest": self.digest,
                "size": self.size, "sha256": self.sha256, "ref": self.ref,
                "birth_artifact_id": self.birth_artifact_id, "extra": dict(self.extra)}.items()
                if value is not None}
        if is_external(self):
            result.update({MARKER: True, "uri": self._reference["uri"]})
        return result

    def ref_url(self):
        parent = self.parent_artifact()
        if parent.id is None:
            raise ValueError("Parent artifact ID is not set")
        return f"backprop-artifact://{quote(parent.id, safe='')}/{quote(self.path, safe='/')}"

    def ref_target(self):
        """Return the captured source URI without reopening a provider.

        Main callers: artifact provenance inspection. Native copied snapshots
        remain independent of their source. Metadata-only external references
        instead require the captured provider when their bytes are downloaded.
        """
        if self.ref is None:
            raise ValueError("Only reference entries support ref_target().")
        return self.ref

    def _read_verified_bytes(self):
        """Read one snapshot and verify it before a caller can decode or publish it."""
        from .external_references import is_external, read_external_reference
        if is_external(self):
            return read_external_reference(self)
        if self.local_path is not None and Path(self.local_path).is_file():
            data = Path(self.local_path).read_bytes()
        elif self._reference is not None and self._parent is not None and self._parent._store is not None:
            data = self._parent._store.download_file(self._reference)
        else:
            raise RuntimeError(f"Artifact content is unavailable: {self.path}")
        if not isinstance(data, bytes):
            raise TypeError("Artifact store.download_file must return bytes")
        if self.size is not None and len(data) != self.size or self.sha256 and hashlib.sha256(data).hexdigest() != self.sha256 or self.digest and bytes_digest(data, self.digest_algorithm()) != self.digest:
            raise ValueError(f"Artifact checksum mismatch: {self.path}")
        return data

    def download(self, root=None, skip_cache=None, executor=None):
        """Download, verify, then atomically publish a file inside the target root.

        Main callers: Artifact.download/file and public API users. Verification
        happens before replacing an existing destination, preserving good copies
        when a transport returns corrupt or truncated content.
        """
        parent = self.parent_artifact()
        directory = Path(root or parent._default_root()).resolve()
        destination = (directory / _logical_path(self.path)).resolve()
        if not destination.is_relative_to(directory):
            raise ValueError("Artifact download path escapes destination")
        from .external_references import is_external
        if destination.is_file() and not is_external(self):
            digest, sha256, size = _checksums(destination, self.digest_algorithm())
            if (self.size is None or size == self.size) and (digest == self.digest or not self.digest and bool(self.sha256)) and (not self.sha256 or sha256 == self.sha256):
                return str(destination)
        data = self._read_verified_bytes()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)


class ArtifactManifest:
    """Read-only manifest view; mutations pass through the owning Artifact."""

    def __init__(self, artifact):
        self._artifact = artifact

    @property
    def entries(self):
        from .deferred_incremental_artifacts import refresh
        refresh(self._artifact)
        return MappingProxyType(self._artifact._entries)

    @property
    def digest_algorithm(self):
        return self._artifact.digest_algorithm

    def digest(self):
        return manifest_digest(self.entries, self.digest_algorithm)

    def get_entry_by_path(self, name):
        return self.entries.get(_logical_path(name))

    def get_entries_in_directory(self, directory):
        prefix = str(PurePosixPath(directory)).rstrip("/") + "/"
        return [entry for name, entry in self.entries.items() if name.startswith(prefix)]

    def to_manifest_json(self):
        return {"version": 1, "contents": {name: entry._storage_json() for name, entry in sorted(self.entries.items())}}


class _ArtifactFilesStore:
    """Adapt verified manifest entries to the existing public File read object."""

    def __init__(self, artifact):
        self.artifact = artifact

    def download_file(self, reference):
        return self.artifact.get_entry(reference["_artifact_name"])._read_verified_bytes()

    def file_url(self, reference):
        entry = self.artifact.get_entry(reference["_artifact_name"])
        from .external_references import is_external
        if is_external(entry):
            return entry._reference["uri"]
        if entry._reference is not None and hasattr(self.artifact._store, "file_url"):
            return self.artifact._store.file_url(entry._reference)
        return entry.ref_url()


class Artifact:
    """A named, immutable collection of files with explicit content identities."""

    def __init__(self, name, type, description=None, metadata=None, incremental=False, use_as=None,
                 storage_region=None, digest_algorithm="MD5"):
        from . import termwarn
        from .artifact_inputs import constructor_name, constructor_identity
        internal = getattr(self, "_allow_reserved", False)
        if not internal:
            constructor_name(name)
        if incremental and not internal:
            termwarn("Using experimental arg `incremental`")
        constructor_identity(name, type, internal=internal)
        self._name, self.type = _collection_name(name), type
        self.description, self.metadata = description, metadata
        self._incremental = incremental
        self._incremental_pending = False
        self._distributed_pending = False
        self._distributed_id = None
        if use_as is not None:
            termwarn("`use_as` argument is deprecated and does not affect the behaviour of `wandb.Artifact()`", repeat=False)
        self._use_as = None
        # Pinned W&B maps exact uppercase literals; unknown/lowercase values
        # select MD5. An exact XXH128 request must never fall back on import failure.
        self._digest_algorithm = {"MD5": ArtifactDigestAlgorithm.MANIFEST_MD5,
            "XXH128": ArtifactDigestAlgorithm.MANIFEST_XXH128}.get(digest_algorithm, ArtifactDigestAlgorithm.MANIFEST_MD5)
        if self._digest_algorithm is ArtifactDigestAlgorithm.MANIFEST_XXH128:
            termwarn("Creating an artifact with the XXH128 digest algorithm."
                     " Calling `artifact.verify()` for this artifact on wandb"
                     " versions before 0.29.0 will fail.")
        hasher(self._digest_algorithm)
        if storage_region is not None:
            raise NotImplementedError("Artifact storage region is controlled by the Foundry file dataset")
        self._entries = {}
        self._temporary = None
        self._objects = {}
        self._encoding_objects = set()
        self._reference_counter = 0
        self._decoded_objects = {}
        self._local_paths = {}
        self._owner = None
        self._store = None
        self._final = False
        self._logged = False
        self._committed = False
        self._journal_count = None
        self._id = uuid.uuid4().hex
        self._version = None
        self._stored_digest = None
        self._entity = self._project = self._run_id = None
        self._created_at = self._history_step = None
        self._aliases, self._tags = [], []
        self.manifest = ArtifactManifest(self)

    @classmethod
    def _new_internal(cls, name, type, **kwargs):
        """Construct SDK-owned snapshots whose reserved names users cannot claim.

        Main callers: Run.log_code and verified manifest reconstruction. The
        public constructor retains its upstream reserved-name checks.
        """
        artifact = cls.__new__(cls)
        artifact._allow_reserved = True
        cls.__init__(artifact, name, type, **kwargs)
        return artifact

    def __setattr__(self, name, value):
        if name in {"name", "type"} and getattr(self, "_final", False):
            raise TypeError("Finalized artifacts are immutable; create a new artifact version")
        object.__setattr__(self, name, value)

    def files(self, names=None, per_page=50, start=None):
        """Return public File objects with stable native pagination cursors.

        Main callers: Api.artifact(...).files. Manifest entries remain available
        through manifest.entries/get_entry; File.download returns a file handle.
        """
        from .apis.public.artifacts import ArtifactFiles
        from .apis.public.files import File
        if not self._logged:
            raise RuntimeError("Log the artifact before listing its public files")
        if self.state != "COMMITTED":
            self.wait()
        if names is not None and (not isinstance(names, list) or any(not isinstance(name, str) for name in names)):
            raise TypeError("names must be a list of artifact-relative filenames")
        store = _ArtifactFilesStore(self)
        def factory():
            for name, entry in sorted(self._entries.items()):
                if names is None or name in names:
                    reference = {**(entry._reference or {"path": name}), "_artifact_name": name,
                                 "size": entry.size, "sha256": entry.sha256,
                                 "digest": entry.digest, "extra": dict(entry.extra),
                                 "mime_type": mimetypes.guess_type(name)[0]}
                    yield File(store, name, reference)
        result = ArtifactFiles(factory, per_page=per_page, start=start,
            scope="artifact-files:" + self.id + ":" + json.dumps(names, sort_keys=True), identity=lambda file: file.name)
        result.path = [self.entity, self.project, self.source_name]
        return result

    @property
    def id(self):
        return self._id if self.state == "COMMITTED" else None

    @property
    def name(self):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        return self._name + ":" + self._version if self._logged and self._version else self._name

    @name.setter
    def name(self, value):
        self._name = _collection_name(value)

    @property
    def metadata(self):
        return self._metadata

    @metadata.setter
    def metadata(self, value):
        """Validate replacements immediately; in-place edits validate on save.

        Main callers: user edits and manifest reconstruction. Normalization
        finishes before assignment, so invalid replacements leave existing
        metadata unchanged. Descriptive edits do not unfreeze artifact files.
        """
        self._metadata = _validate_metadata(value)

    @property
    def digest(self):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        return self._stored_digest or self._content_digest(self._tags)

    def _content_digest(self, tags):
        """Hash all immutable public content, including metadata and description.

        Main callers: digest and _log_to_run. Equal file bytes with different
        metadata must never resolve to an ambiguous immutable version name.
        """
        metadata = _validate_metadata(self.metadata)
        identity = {"name": self.name, "type": self.type, "description": self.description,
            "metadata": metadata, "tags": list(tags),
            "files": {name: {"digest": entry.digest, "size": entry.size, "extra": dict(entry.extra)}
                      for name, entry in sorted(self._entries.items())}}
        from .external_references import is_external
        for name, entry in self._entries.items():
            if is_external(entry):
                identity["files"][name]["uri"] = entry._reference["uri"]
        if self.digest_algorithm is ArtifactDigestAlgorithm.MANIFEST_XXH128:
            identity["digest_algorithm"] = self.digest_algorithm.value
        content = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(content.encode()).hexdigest()

    @property
    def version(self):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        return self._version

    @property
    def state(self):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        if self._committed:
            return "COMMITTED"
        if getattr(self, '_contribution_handle', None) is not None:
            return "PENDING"
        journal = getattr(self._owner, "_journal", None)
        if self._logged and journal is not None and self._journal_count is not None:
            if journal.ack >= self._journal_count or journal.acknowledged_count() >= self._journal_count:
                # Commit is permanent even if a caller later removes its local
                # queue. Another process's sync becomes visible at this read.
                self._committed = True
                return "COMMITTED"
        from .deferred_incremental_artifacts import restored_acknowledged
        if self._logged and restored_acknowledged(self):
            self._committed = True
            return "COMMITTED"
        return "PENDING"

    @property
    def size(self):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        return sum(entry.size or 0 for entry in self._entries.values())

    @property
    def entity(self):
        return self._entity

    @property
    def project(self):
        return self._project

    @property
    @ensure_logged
    def aliases(self):
        return self._aliases

    @aliases.setter
    @ensure_logged
    def aliases(self, aliases):
        from .artifact_updates import validate_aliases
        self._aliases = validate_aliases(aliases)

    @property
    def tags(self):
        return self._tags

    @tags.setter
    def tags(self, values):
        from .artifact_updates import validate_tags
        self._tags = validate_tags(values)

    @property
    def qualified_name(self):
        return "/".join(part for part in [self.entity, self.project, self.name] if part)

    @property
    def source_name(self):
        return self.name

    source_version = property(lambda self: self.version)
    source_entity = property(lambda self: self.entity)
    source_project = property(lambda self: self.project)
    file_count = property(lambda self: len(self._entries))
    @property
    @ensure_logged
    def commit_hash(self):
        from .artifact_identity import commit_hash
        return commit_hash(self)

    digest_algorithm = property(lambda self: self._digest_algorithm)
    created_at = property(lambda self: self._created_at)
    updated_at = property(lambda self: self._created_at)
    history_step = property(lambda self: self._history_step)
    @property
    def use_as(self):
        """Retain the deprecated value and its pinned one-time warning."""
        from . import termwarn
        termwarn("The use_as property of Artifact is deprecated.", repeat=False)
        return self._use_as

    source_artifact = property(lambda self: self)
    source_qualified_name = property(lambda self: self.qualified_name)
    # Registry properties are installed as explicit unsupported guards.

    def _read_api(self):
        from .apis.public import Api
        if self._store is None or not self._logged:
            raise RuntimeError("An authenticated logged artifact is required")
        return Api._from_store(self._store, entity=self.entity, project=self.project)

    @property
    def collection(self):
        return self._read_api().artifact_collection(self.type, "/".join([self.entity or "", self.project, self._name]))

    source_collection = property(lambda self: self.collection)

    def logged_by(self):
        if self._run_id is None:
            return None
        return self._read_api().run("/".join([self.entity or "", self.project, self._run_id]))

    def used_by(self):
        api = self._read_api()
        identities = set()
        for event in self._store.read_events():
            payload = event["payload"]
            matches_source = (payload.get("source_entity", event.get("entity") or "") == (self.entity or "")
                and payload.get("source_project", event["project"]) == self.project
                and payload.get("artifact_type") == self.type
                and payload.get("name", "").removesuffix(":" + str(payload.get("version", ""))) == self._name)
            if event.get("type") == "artifact_use" and payload.get("artifact_id") == self.id and matches_source:
                identities.add((event.get("entity") or "", event["project"], event["run_id"]))
        return [api.run("/".join(identity)) for identity in sorted(identities)]

    @property
    def url(self):
        run = self.logged_by()
        if run is None:
            raise RuntimeError("Artifact has no originating run URL")
        return run.url

    def is_draft(self):
        return self.state == "PENDING"

    def __repr__(self):
        return f"<Artifact {self.id or self.name}>"

    def _check_mutable(self):
        if self._final:
            raise RuntimeError("Artifact is finalized; create a new artifact version to change files")

    def _temporary_root(self):
        """Allocate scratch storage only for local writes, never remote listings."""
        if self._temporary is None:
            self._temporary = Path(tempfile.mkdtemp(prefix="backprop-artifact-"))
            weakref.finalize(self, shutil.rmtree, self._temporary, ignore_errors=True)
        return self._temporary

    def add_file(self, local_path, name=None, is_tmp=False, skip_cache=False, policy="mutable", overwrite=False):
        """Snapshot source bytes now and return their immutable manifest entry.

        Main callers: training code, add_dir, and new_file. Both supported policy
        values keep a private copy because delayed Foundry upload must never read
        a different generation of a caller's file.
        """
        self._check_mutable()
        if policy not in {None, "mutable", "immutable"}:
            raise ValueError("policy must be mutable or immutable")
        source = Path(local_path)
        if not source.is_file():
            raise ValueError(f"Path is not a file: {local_path!r}")
        logical = _logical_path(name or source.name)
        snapshot = self._temporary_root() / (uuid.uuid4().hex + source.suffix)
        with source.open("rb") as src, snapshot.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        digest, sha256, size = _checksums(snapshot, self.digest_algorithm)
        if is_tmp:
            parent, filename = PurePosixPath(logical).parent, PurePosixPath(logical).name
            pieces = filename.split(".")
            pieces[0] = base64.b64decode(digest).hex()[:20]
            logical = str(parent / ".".join(pieces))
            if logical in self._entries and not overwrite:
                snapshot.unlink()
                return self._entries[logical]
        existing = self._entries.get(logical)
        if existing is not None and not overwrite:
            snapshot.unlink()
            if existing.digest == digest and existing.size == size:
                return existing
            raise ValueError(f"Artifact entry {logical} already contains different bytes; pass overwrite=True")
        extra = {"alg": "XXH128"} if self.digest_algorithm is ArtifactDigestAlgorithm.MANIFEST_XXH128 else {}
        entry = ArtifactManifestEntry(path=logical, digest=digest, size=size, sha256=sha256, extra=extra,
            local_path=str(snapshot), skip_cache=bool(skip_cache), _parent=self)
        self._entries[logical] = entry
        self._local_paths[str(local_path)] = logical
        return entry

    def add_dir(self, local_path, name=None, skip_cache=False, policy="mutable", merge=False):
        self._check_mutable()
        root = Path(local_path)
        if not root.is_dir():
            raise ValueError(f"Path is not a directory: {local_path!r}")
        planned, visited = [], set()
        for directory, dirs, files in os.walk(root, followlinks=True):
            resolved = Path(directory).resolve()
            if resolved in visited:
                raise ValueError("Artifact directory has a symlink cycle or repeated directory target")
            visited.add(resolved)
            dirs.sort()
            for filename in sorted(files):
                path = Path(directory) / filename
                planned.append((path, _logical_path(str(PurePosixPath(name or "") / path.relative_to(root).as_posix()))))
        original, original_paths = dict(self._entries), dict(self._local_paths)
        try:
            for path, logical in planned:
                self.add_file(path, logical, skip_cache=skip_cache, policy=policy, overwrite=merge)
        except BaseException:
            self._entries = original
            self._local_paths = original_paths
            raise

    @contextmanager
    def new_file(self, name, mode="x", encoding=None, policy="mutable"):
        self._check_mutable()
        logical = _logical_path(name)
        if "x" in mode and logical in self._entries:
            raise ValueError(f"File with name {logical!r} already exists")
        path = self._temporary_root() / "new" / logical
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding=encoding) as stream:
            yield stream
        self.add_file(path, logical, policy=policy, skip_cache=True, overwrite="x" not in mode)

    def remove(self, item):
        self._check_mutable()
        logical = item.path if isinstance(item, ArtifactManifestEntry) else str(PurePosixPath(item))
        names = [name for name in self._entries if name == logical or name.startswith(logical.rstrip("/") + "/")]
        if not names:
            raise FileNotFoundError(logical)
        for name in names:
            del self._entries[name]

    def get_entry(self, name):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        return self._entries[_logical_path(name)]

    get_path = get_entry

    def get_added_local_path_name(self, local_path):
        return self._local_paths.get(str(local_path))

    def _set_entry_provenance(self, entry, *, ref=None, birth_artifact_id=None):
        """Publish provenance together with an already verified local snapshot.

        Main callers: add_reference and new_draft. Replacing this draft entry
        leaves its source immutable and preserves the digest of the copied
        bytes. Provenance never changes the content-derived version hash.
        """
        captured = replace(entry, ref=ref, birth_artifact_id=birth_artifact_id,
                           _public_fields_set=entry._public_fields_set | {"ref", "birth_artifact_id"})
        self._entries[entry.path] = captured
        return captured

    def _copy_entry(self, source, name=None, *, ref=None):
        """Copy stored bytes or reference metadata, retaining known origins.

        Main callers: add_reference and new_draft. This tracks explicit ancestry,
        not a global guess based on equal file hashes. A standalone entry without
        known ancestry receives its own artifact's birth ID when logged.
        """
        from .external_references import is_external
        if is_external(source):
            path = _logical_path(name or source.path)
            birth = source.birth_artifact_id
            if birth is None and source._parent is not None and source._parent._logged:
                birth = source._parent._id
            entry = replace(source, path=path, ref=ref, _parent=self,
                            birth_artifact_id=birth,
                            _reference=_thaw(source._reference),
                            _public_fields_set=source._public_fields_set | {"path", "ref", "birth_artifact_id"})
            existing = self._entries.get(path)
            if existing is not None and existing._storage_json() != entry._storage_json():
                raise ValueError(f"Artifact entry {path} already contains a different reference")
            self._entries[path] = entry
            return entry
        data = source._read_verified_bytes()
        temporary = self._temporary_root() / ("ref-" + uuid.uuid4().hex)
        temporary.write_bytes(data)
        try:
            entry = self.add_file(temporary, name or source.path)
            parent = source._parent
            birth = source.birth_artifact_id
            if birth is None and parent is not None and parent._logged:
                birth = parent._id
            return self._set_entry_provenance(entry, ref=ref, birth_artifact_id=birth)
        finally:
            temporary.unlink(missing_ok=True)

    def add_reference(self, uri, name=None, checksum=True, max_objects=None):
        """Track provider metadata or copy an authenticated native snapshot.

        Main callers: training code, Run.log_artifact(uri), and artifact media.
        External references keep their bytes at the provider, including when
        checksum=False; only immutable URI/metadata is uploaded to Foundry.
        Native artifact references preserve their original storage kind.
        """
        self._check_mutable()
        if isinstance(uri, ArtifactManifestEntry):
            reference = uri.ref
            if not reference or urlsplit(reference).scheme != "backprop-artifact":
                parent = uri._parent
                if parent is not None and parent.id is not None:
                    reference = uri.ref_url()
            return [self._copy_entry(uri, name, ref=reference)]
        if not isinstance(uri, str):
            raise TypeError("Artifact reference must be a URI string or ArtifactManifestEntry")
        if urlsplit(uri).scheme == "backprop-artifact":
            from .artifact_references import native_reference_entry
            return self.add_reference(native_reference_entry(self, uri), name=name,
                                      checksum=checksum, max_objects=max_objects)
        from .external_references import add_external_reference
        return add_external_reference(self, uri, name, checksum, max_objects)

    def add(self, obj, name, overwrite=False):
        """Snapshot a supported typed value and its media dependencies together.

        Main callers: training code and __setitem__. Object encoding is separate
        from Run logging: it always snapshots the whole Table and never consumes
        a run's incremental Table cursor.
        """
        from .object_codec import object_suffix, encode_object
        self._check_mutable()
        suffix = object_suffix(obj)
        logical = _logical_path(name)
        logical = logical if logical.endswith(suffix) else logical + suffix
        if id(obj) in self._objects:
            return self._objects[id(obj)][1]
        if id(obj) in self._encoding_objects:
            raise ValueError("Artifact Table relationships cannot form a cycle")
        if logical in self._entries and not overwrite:
            return self._entries[logical]
        entries, paths, objects = dict(self._entries), dict(self._local_paths), dict(self._objects)
        self._encoding_objects.add(id(obj))
        try:
            payload = encode_object(obj, self)
            # Logical overwrite validation happened above. A failed enclosing
            # object transaction can leave scratch JSON after manifest rollback;
            # replace that scratch file on retry. Manifest entries hold separate
            # immutable snapshots, so this cannot alter prior committed bytes.
            with self.new_file(logical, mode="w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            entry = self._entries[logical]
        except BaseException:
            self._entries, self._local_paths, self._objects = entries, paths, objects
            raise
        finally:
            self._encoding_objects.remove(id(obj))
        self._objects[id(obj)] = (obj, entry)
        # Nested relationship encoding can still fail after a child succeeded.
        # Publish provenance only at the outer transaction boundary. Implicit
        # run artifacts defer it further until their history event is durable.
        if not self._encoding_objects and not getattr(self, "_defer_provenance", False):
            for value, captured in self._objects.values():
                if getattr(value, "_artifact_target", None) is None:
                    value._artifact_target, value._artifact_target_name = self, captured.path
        return entry

    def get(self, name):
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        from .object_codec import object_suffixes, decode_object
        from .values import WBValue
        suffixes = object_suffixes()
        logical = _logical_path(name)
        entry = next((self._entries[candidate] for candidate in [logical, *[logical + suffix for suffix in suffixes]]
                      if candidate in self._entries and any(candidate.endswith(suffix) for suffix in suffixes)), None)
        if entry is None:
            return None
        if entry.path in self._decoded_objects:
            return self._decoded_objects[entry.path]
        payload = json.loads(entry._read_verified_bytes())
        selected = next((cls for cls in WBValue.type_mapping().values()
                         if cls.with_suffix(logical) == entry.path), None)
        if selected is not None and not selected.__module__.startswith("backprop."):
            result = selected.from_json(payload, self)
        else:
            result = decode_object(payload, self)
        if isinstance(result, WBValue):
            result._artifact_source, result._artifact_source_name = self, entry.path
        self._decoded_objects[entry.path] = result
        return result

    def __getitem__(self, name):
        return self.get(name)

    def __setitem__(self, name, obj):
        return self.add(obj, name)

    def finalize(self):
        """Freeze file operations while retaining editable descriptive fields.

        Main callers: explicit finalization, logging and manifest restoration.
        Metadata is validated again at publication/save; immutable version
        identity remains fixed once the manifest has been published.
        """
        self._final = True

    def _log_to_run(self, run, aliases=None, tags=None):
        """Stage immutable bytes and append a manifest before changing state.

        Main callers: Run.log_artifact. Version IDs include immutable metadata
        and all file identities. Aliases are editable events separate from immutable file identity.
        """
        if self._logged:
            if self._owner is run:
                return self
            raise RuntimeError("This Artifact is already logged; create a new Artifact for another version")
        payload = self._prepare_log(run, aliases, tags)
        with run._lock:
            run._emit("artifact", payload)
            self._commit_log(run, payload)
        return self

    @property
    def incremental(self): return self._incremental

    @property
    def distributed_id(self): return self._distributed_id
    @distributed_id.setter
    def distributed_id(self, distributed_id):
        # This is a local descriptor, including after finalization. Publication
        # validates group values at its own boundary before recording intent.
        self._distributed_id = distributed_id

    def _prepare_log(self, run, aliases=None, tags=None):
        """Stage a manifest without publishing it or changing public state.

        Main callers: explicit log_artifact and implicit table preparation.
        The latter embeds the manifest in its owning value event so invalid or
        overwritten pending rows cannot publish orphan artifacts.
        """
        from .artifact_updates import logging_aliases
        aliases = logging_aliases(aliases)
        from .artifact_logging import publication_tags
        tags = publication_tags(tags) or []
        metadata = _validate_metadata(self.metadata)
        if not self._final:
            self.metadata = metadata
        files = []
        if run._journal is not None:
            from .external_references import is_external
            for name, entry in self._entries.items():
                if is_external(entry):
                    reference = {**entry._storage_json(), "name": name}
                elif entry.local_path is not None:
                    reference = run._journal.stage_file(entry.local_path, name=name)
                elif entry._reference is not None:
                    # An incremental base can reuse an existing immutable blob.
                    reference = {**_thaw(entry._reference), "name": name}
                else:
                    raise RuntimeError(f"Artifact content is unavailable: {name}")
                files.append({**reference, "digest": entry.digest, "extra": dict(entry.extra),
                              "birth_artifact_id": entry.birth_artifact_id or self._id,
                              "ref": entry.ref})
        digest = self._content_digest(tags)
        version = "sha256-" + digest
        payload = {"artifact_id": self._id, "name": self.name, "artifact_type": self.type,
                   "version": version, "digest": digest, "digest_algorithm": self.digest_algorithm.value,
                   "description": self.description,
                   "metadata": metadata, "aliases": aliases, "tags": tags,
                   "files": files, "state": "COMMITTED", "manifest_version": 1,
                   "created_at": datetime.now(timezone.utc).isoformat(), "history_step": run.step}
        if self._incremental_pending:
            payload["_backprop_incremental"] = {"version": 1}
        if getattr(self, "_incremental_base_id", None):
            payload["incremental_base_id"] = self._incremental_base_id
        return payload

    def _commit_log(self, run, payload):
        """Finalize only after the explicit or containing event was appended."""
        self._contribution_handle = None
        self._journal_count = run._journal.count if run._journal is not None else None
        self._owner, self._store = run, run._sink
        self._entity, self._project, self._run_id = run.entity, run.project, run.id
        self._distributed_id = payload.get("distributed_id")
        self._version, self._aliases, self._tags = payload["version"], payload["aliases"], payload["tags"]
        self._stored_digest = payload["digest"]
        self._created_at, self._history_step = payload["created_at"], payload["history_step"]
        # Assign only after the manifest is durable. Entry handles already
        # returned by add_file observe the same committed provenance, while a
        # failed append leaves both the draft and its handles unchanged.
        for reference in payload["files"]:
            entry = self._entries[reference["name"]]
            object.__setattr__(entry, "birth_artifact_id", reference.get("birth_artifact_id"))
        self.finalize()
        self._logged = True
        from .artifact_updates import enable_edits
        enable_edits(self)
        return self

    @classmethod
    def _from_manifest(cls, payload, store, entity=None, project=None, run_id=None):
        """Rebuild an immutable read object without eagerly downloading files."""
        algorithm = ArtifactDigestAlgorithm(payload.get("digest_algorithm", "MANIFEST_MD5"))
        artifact = cls._new_internal(payload["name"], payload["artifact_type"],
            description=payload.get("description"), metadata=payload.get("metadata"),
            digest_algorithm="XXH128" if algorithm is ArtifactDigestAlgorithm.MANIFEST_XXH128 else "MD5")
        artifact._id = payload.get("artifact_id") or payload.get("digest") or artifact._id
        artifact._distributed_id = payload.get("distributed_id")
        artifact._incremental_pending = bool(payload.get("_backprop_incremental"))
        artifact._distributed_pending = bool(payload.get("_backprop_distributed"))
        artifact._incremental = artifact._incremental_pending
        artifact._version = payload.get("version")
        artifact._stored_digest = payload.get("digest")
        artifact._entity, artifact._project, artifact._run_id = entity, project, run_id
        artifact._aliases, artifact._tags = list(payload.get("aliases", [])), list(payload.get("tags", []))
        artifact._store = store
        artifact._created_at, artifact._history_step = payload.get("created_at"), payload.get("history_step")
        for ref in payload.get("files", []):
            name = _logical_path(ref.get("name") or ref["path"])
            artifact._entries[name] = ArtifactManifestEntry(path=name, digest=ref.get("digest", ""), size=ref.get("size"), sha256=ref.get("sha256"),
                ref=ref.get("ref"), birth_artifact_id=ref.get("birth_artifact_id"),
                extra=dict(ref.get("extra", {})), _parent=artifact, _reference=dict(ref))
        artifact.finalize()
        artifact._logged = True
        artifact._committed = not (artifact._incremental_pending or artifact._distributed_pending)
        from .artifact_updates import enable_edits
        enable_edits(artifact)
        return artifact

    def _reference_json(self):
        if not self._logged:
            raise RuntimeError("Log an artifact before referring to it in configuration")
        return {"_type": "artifact", "artifact_id": self._id, "name": self.name,
                "version": self.version, "artifact_type": self.type, "digest": self.digest,
                "qualified_name": self.qualified_name, "source_entity": self.source_entity,
                "source_project": self.source_project}

    def wait(self, timeout=None):
        """Wait for acknowledgement, returning control at the caller's deadline.

        Main callers: training scripts after Run.log_artifact/upsert_artifact and
        public file retrieval. Contributions stay pending after acceptance until
        a later wait observes the primary's completed union in the read view.
        A timed-out caller does not cancel an already accepted remote
        write. The journal worker retains its upload lock until that request
        finishes, commits any successful acknowledgement, then stops at the
        deadline before another batch. Retries keep the original event IDs.
        """
        from .artifact_identity import ArtifactNotLoggedError, WaitTimeoutError
        if self.state == "COMMITTED":
            return self
        contribution = getattr(self, '_contribution_handle', None)
        journal = contribution.journal if contribution is not None else getattr(self._owner, '_journal', None)
        if (not self._logged and contribution is None) or journal is None or journal.sink is None:
            raise ArtifactNotLoggedError('Artifact.wait', self)
        message = 'Artifact upload wait timed out, failed to fetch Artifact response'
        if timeout is not None and timeout <= 0:
            raise WaitTimeoutError(message)
        from .runtime import bounded_call
        deadline = None if timeout is None else time.monotonic() + timeout
        def upload_and_read():
            journal.flush(deadline=deadline)
            if contribution is not None:
                from .artifact_contributions import completed
                return completed(contribution)
        try:
            completion = bounded_call(upload_and_read, timeout, "artifact wait")
        except TimeoutError as error:
            raise WaitTimeoutError(message) from error
        if contribution is not None:
            if getattr(self, '_contribution_handle', None) is not contribution:
                raise RuntimeError('Artifact contribution changed while waiting; retry its current publication')
            self._pending_contribution = None
            if completion is not None:
                from .artifact_contributions import adopt
                adopt(self, contribution, completion)
            return self
        if self.state != "COMMITTED":
            raise RuntimeError("Artifact upload has not been acknowledged")
        return self

    def new_draft(self):
        if self.state != "COMMITTED":
            raise RuntimeError("Wait for a committed artifact before making a new draft")
        draft = Artifact._new_internal(self._name, self.type, description=self.description, metadata=_thaw(self.metadata),
            digest_algorithm="XXH128" if self.digest_algorithm is ArtifactDigestAlgorithm.MANIFEST_XXH128 else "MD5")
        draft._entity, draft._project = self.entity, self.project
        for entry in self._entries.values():
            # A new draft copies the manifest's existing origin fields; it does
            # not turn an ordinary stored file into a new reference entry.
            draft._copy_entry(entry, ref=entry.ref)
        return draft

    def save(self, project=None, settings=None):
        """Publish a draft, or persist descriptive edits after acknowledgement.

        Main callers: explicit Artifact.save and automatic artifact workflows.
        A queued offline/online object is still a draft until its upload commits.
        The latest initialized active run owns draft publication, including runs
        created with create_new that deliberately do not replace module globals.
        With no active run, create and finish a silent automatic run; explicit
        settings replace that default. Committed edits ignore project/settings.
        """
        import backprop
        if not self.is_draft():
            from .artifact_updates import save_artifact
            return save_artifact(self)
        # Disabled handles are globally bound but never join the upstream
        # active-run registry. They cannot swallow draft publication here.
        active = next((run for run in reversed(backprop._runs) if not run._closed and not run.disabled), None)
        if active is not None:
            active.log_artifact(self)
        else:
            if settings is None:
                settings = backprop.Settings(silent=True)
            with backprop.init(entity=self.entity, project=project or self.project, job_type="auto", settings=settings) as run:
                run.log_artifact(self)

    @ensure_logged
    def json_encode(self):
        from .artifact_identity import json_encode
        return json_encode(self)

    def _default_root(self, *, collection_only=False):
        """Confine identity-derived paths before download or checkout mutates disk.

        Main callers: download, file, verify, entry download, and checkout. Names
        accepted as remote identities can be unsafe as local paths. Callers can
        still supply an explicit root, but automatic paths must stay beneath
        artifacts/, including when an existing collection directory is a symlink.
        """
        # Upstream permits single-character collection names before a colon.
        # Validate the suffix too, then enforce the resolved directory boundary
        # below so Windows drive syntax cannot escape the chosen root.
        checked = self._name[2:] if len(self._name) > 1 and self._name[1] == ":" else self._name
        _logical_path(checked)
        root = Path("artifacts").resolve()
        destination = (root / (self._name if collection_only else self.name)).resolve()
        if destination == root or not destination.is_relative_to(root):
            raise ValueError("Artifact collection name escapes the default download directory")
        return str(destination)

    def download(self, root=None, allow_missing_references=False, skip_cache=None, path_prefix=None, multipart=None):
        directory = Path(root or self._default_root())
        directory.mkdir(parents=True, exist_ok=True)
        self._download_entries(directory, allow_missing_references, skip_cache, path_prefix)
        return str(directory)

    def _download_entries(self, directory, allow_missing_references=False, skip_cache=None, path_prefix=None):
        """Return only explicit source-defined omissions after successful reads.

        Main callers: public download and checkout's staging pass. Checkout must
        distinguish an intentionally skipped GCS directory from a missing staged
        file, so it can reject unexpected data loss before changing destination.
        """
        from .deferred_incremental_artifacts import refresh
        refresh(self)
        from .reference_handlers.gcs_handler import _GCSIsADirectoryError
        prefix = str(path_prefix or "")
        skipped = set()
        for name, entry in self._entries.items():
            if name.startswith(prefix):
                try:
                    entry.download(directory, skip_cache=skip_cache)
                except FileNotFoundError as error:
                    if not allow_missing_references:
                        raise
                    from . import termwarn
                    termwarn(str(error))
                    skipped.add(name)
                except _GCSIsADirectoryError:
                    # The pinned GCS handler explicitly identifies directory
                    # references; an artifact download omits their byte reads.
                    skipped.add(name)
        return skipped

    def checkout(self, root=None):
        """Replace local directory contents with this verified immutable version.

        Main callers: explicit Artifact.checkout. Download all bytes first so a
        transport failure cannot delete unrelated existing files prematurely.
        """
        from .apis.public.files import safe_destination, write_atomic
        directory = Path(root if root is not None else self._default_root(collection_only=True)).resolve()
        with tempfile.TemporaryDirectory(prefix="backprop-checkout-") as stage:
            skipped = self._download_entries(Path(stage))
            downloaded = [name for name in self._entries if name not in skipped]
            for name in downloaded:
                if not (Path(stage) / name).is_file():
                    raise FileNotFoundError(f"Artifact staged file is missing: {name}")
            directory.mkdir(parents=True, exist_ok=True)
            for path in directory.rglob("*"):
                if (path.is_file() or path.is_symlink()) and path.relative_to(directory).as_posix() not in self._entries:
                    path.unlink()
            # A successful download can intentionally omit a GCS directory
            # reference. Publish the actual staged files, preserving existing
            # tracked paths for those source-defined omissions.
            for name in downloaded:
                destination = directory / name
                if destination.is_symlink():
                    destination.unlink()
                destination = safe_destination(directory, name)
                write_atomic(destination, (Path(stage) / name).read_bytes())
        return str(directory)

    def file(self, root=None):
        if len(self._entries) != 1:
            raise ValueError("Artifact.file requires exactly one entry; use download() for a collection")
        return next(iter(self._entries.values())).download(root)

    def verify(self, root=None):
        directory = Path(root or self._default_root())
        actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        required = {name for name, entry in self._entries.items() if entry.ref is None}
        if not required.issubset(actual) or not actual.issubset(self._entries):
            raise ValueError("Artifact directory has missing or unexpected files")
        for name, entry in self._entries.items():
            if entry.ref is not None:
                continue
            digest, sha256, size = _checksums(directory / name, entry.digest_algorithm())
            if size != entry.size or entry.digest and digest != entry.digest or entry.sha256 and sha256 != entry.sha256:
                raise ValueError(f"Digest mismatch for file: {name}")
        references = sum(entry.ref is not None for entry in self._entries.values())
        if references:
            from . import termwarn
            termwarn(f"skipped verification of {references} refs")
