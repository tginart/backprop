"""Artifact collection metadata reconstructed from immutable manifest events.

Architecture: group manifests by project/type/name -> read collections and
versions -> the same Artifact class used for local snapshots. Descriptive edits and aliases are replayed as separate events; deletion and
registry administration remain excluded. Main callers: Public Api.
"""
from __future__ import annotations

from backprop.errors import UnsupportedFeatureError

import hashlib
import copy

from ..normalize import normalize_exceptions
from .query import Collection
from .projects import project_identity, project_internal_identity


class Artifacts(Collection): pass
class RunArtifacts(Collection): pass
class ArtifactTypes(Collection): pass
class ArtifactCollections(Collection): pass
class ProjectArtifactCollections(ArtifactCollections): pass
class ArtifactFiles(Collection): pass


class ArtifactType:
    def __init__(self, api, entity, project, name):
        self._api, self.entity, self.project, self.name = api, entity, project, name

    @property
    def id(self): return hashlib.sha256(f"{self.entity}/{self.project}/{self.name}".encode()).hexdigest()

    @normalize_exceptions
    def collections(self, filters=None, order=None, per_page=50, start=None):
        return self._api._collections(self.entity, self.project, self.name,
                                      filters=filters, order=order, per_page=per_page, start=start)

    def collection(self, name):
        return self._api.artifact_collection(self.name, f"{self.entity}/{self.project}/{name}")

    def load(self): return {"id": self.id, "name": self.name}

    def __repr__(self): return f"<ArtifactType {self.entity}/{self.project}/{self.name}>"


class ArtifactCollection:
    def __init__(self, api, entity, project, name, type, events, *, record=None):
        self._api, self._entity, self._project, self.name, self.type = api, entity, project, name, type
        self._events = events
        if record is None and not events:
            from ...artifact_collections import collection_identity
            # An empty lazy collection has no creation evidence. Construction
            # must not fetch data; iteration/load supplies the actual snapshot,
            # and save still requires a visible original collection.
            record = {'id': collection_identity(entity, project, name, type),
                      'description': None, 'tags': [], 'created_at': None, 'updated_at': None}
        elif record is None:
            from ...artifact_collections import collection_catalog
            records, members = collection_catalog(api._store.read_events(entity=entity, project=project))
            identifier = events[0]['payload'].get('_collection_id') or members[(entity, project, events[0]['payload']['artifact_id'])]
            record = records[identifier]
        self._id = record['id']
        self._edits = {key: copy.deepcopy(record[key]) for key in ('description', 'tags')}
        self._created_at, self._updated_at = record['created_at'], record['updated_at']
        self._saved = self._values()

    @property
    def id(self): return self._id
    @property
    def entity(self): return self._entity
    @property
    def project(self): return self._project
    @property
    def name(self): return self._name
    @name.setter
    def name(self, value):
        if not isinstance(value, str) or '/' in value or len(value) > 128:
            raise ValueError('Collection name must be a string of at most 128 characters without /')
        self._name = value
    @property
    def type(self): return self._type
    @type.setter
    def type(self, value):
        if not isinstance(value, str): raise TypeError('Collection type must be a string')
        self._type = value
    @property
    def project_id(self): return project_identity(self.entity, self.project)
    @property
    def project_internal_id(self): return project_internal_identity(self.entity, self.project, encoded=True)
    @property
    def created_at(self): return self._created_at
    @property
    def updated_at(self): return self._updated_at
    @property
    def description(self): return self._edits['description']
    @description.setter
    def description(self, value):
        if value is not None and not isinstance(value, str):
            raise TypeError('Collection description must be a string or None')
        self._edits["description"] = value
    @property
    def tags(self):
        return self._edits['tags']
    @tags.setter
    def tags(self, value):
        from ...artifact_updates import validate_tags
        self._edits["tags"] = validate_tags(value)
    @property
    def aliases(self):
        return sorted({alias for event in self._events for alias in event["payload"].get("aliases", [])})
    def is_sequence(self): return True

    @normalize_exceptions
    def artifacts(self, per_page=50, start=None):
        return self._api.artifacts(self.type, f"{self.entity}/{self.project}/{self.name}", per_page=per_page, start=start)

    def _record(self):
        return {"name": self.name, "type": self.type, "description": self.description,
                "tags": self.tags, "createdAt": self.created_at, "updatedAt": self.updated_at}

    def load(self, entity, project, type_, name):
        latest = self._api.artifact_collection(type_, f"{entity}/{project}/{name}")
        self._entity, self._project, self.type, self.name = entity, project, type_, name
        self._events, self._edits = latest._events, latest._edits
        self._id, self._saved = latest._id, latest._saved
        self._created_at, self._updated_at = latest._created_at, latest._updated_at
        return self._record()

    def _readonly(self, *args, **kwargs): raise UnsupportedFeatureError("Artifact collections are read-only immutable snapshots")
    delete = change_type = _readonly

    def _values(self):
        return copy.deepcopy(dict(name=self.name, artifact_type=self.type,
                                  description=self.description, tags=self.tags))

    @normalize_exceptions
    def save(self):
        """Journal changed collection fields while keeping file identities intact.

        Main callers: public experiment management. Read-only project ownership
        follows the pinned object. Stable IDs route stale editors after a rename;
        only changed fields are written so unrelated concurrent edits survive.
        """
        from ...artifact_inputs import constructor_identity
        from ...artifact_updates import validate_tags
        from ...artifact_collections import collection_catalog
        if not isinstance(self.name, str) or '/' in self.name or len(self.name) > 128:
            raise ValueError('Collection name must be a string of at most 128 characters without /')
        if not isinstance(self.type, str):
            raise TypeError('Collection type must be a string')
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError('Collection description must be a string or None')
        if self.type != self._saved['artifact_type']:
            constructor_identity(self.name, self.type)
            constructor_identity(self.name, self._saved['artifact_type'])
        values = self._values()
        values['tags'] = validate_tags(values['tags'])
        changes = {key: value for key, value in values.items() if value != self._saved[key]}
        if not changes: return
        records, _ = collection_catalog(self._api._store.read_events(entity=self.entity, project=self.project))
        if self.id not in records: raise ValueError('Collection has no visible original to edit')
        current = records[self.id]
        destination = (changes.get('name', current['name']), changes.get('artifact_type', current['artifact_type']))
        if any(record['id'] != self.id and (record['name'], record['artifact_type']) == destination for record in records.values()):
            raise ValueError('Collection destination already exists')
        from .mutations import writer_for
        writer_for(self._api, self.entity, self.project, "__project__").write("collection_update", {
            "collection_id": self.id, "name": self._saved['name'],
            "artifact_type": self._saved['artifact_type'], "values": changes})
        self._saved = values
        self._api.flush()

    def __repr__(self): return f"<ArtifactCollection {self.entity}/{self.project}/{self.name} ({self.type})>"
