"""Experiment project views derived from existing run events.

Architecture: project namespaces are discovered from run_start events; returned
objects link to the same Foundry workspace and expose artifact read collections.
There is no extra project registry. Main callers: Api.projects and Api.project.
"""
from backprop.errors import UnsupportedFeatureError

import base64
import hashlib
import html
from urllib.parse import urlencode

from ..normalize import normalize_exceptions
from .query import Collection
from .display import Displayable


def project_identity(entity, name):
    """Return the existing deterministic project ID without another registry.

    Main callers: Project.id and artifact/run project identity properties.
    The entity/project pair is the recorded namespace. Metadata edits, event
    order and process restarts therefore cannot change its identifier.
    """
    return hashlib.sha256(f"{entity}/{name}".encode()).hexdigest()


def project_internal_identity(entity, name, *, encoded=False):
    """Expose one project identity in the two upstream public return types.

    Main callers: Public Run.project_internal_id (integer) and
    ArtifactCollection.project_internal_id (encoded string). These are Backprop
    namespace IDs, not W&B database keys or Foundry resource RIDs. The encoded
    form identifies its backend explicitly, rather than claiming a W&B ID.
    """
    value = int(project_identity(entity, name), 16)
    return base64.b64encode(f"BackpropProject:{value}".encode()).decode() if encoded else value


class Projects(Collection):
    @property
    def length(self): return None


class Project(Displayable):
    def __init__(self, api, entity, name, events):
        self._api, self.entity, self.name = api, entity, name
        self._events = events

    @property
    def id(self): return project_identity(self.entity, self.name)
    @property
    def path(self): return [self.entity, self.name]
    @property
    def url(self): return self._api._config.get("app_url", "").rstrip("/") + "/?" + urlencode({"project": "/".join(self.path)})
    @property
    def created_at(self): return min(event["timestamp"] for event in self._events)
    @property
    def updated_at(self): return max(event["timestamp"] for event in self._events)
    @property
    def owner(self): raise UnsupportedFeatureError("Foundry authentication replaces W&B account and user objects")

    @normalize_exceptions
    def artifacts_types(self, per_page=50):
        values = self._api.artifact_types("/".join(self.path))
        return type(values)(values._factory, per_page)

    @normalize_exceptions
    def collections(self, filters=None, order=None, per_page=50):
        return self._api._collections(self.entity, self.name, filters=filters, order=order, per_page=per_page)

    def to_html(self, height=420, hidden=False):
        if not isinstance(height, int) or height <= 0: raise ValueError("height must be a positive integer")
        style = f"border:none;width:100%;height:{height}px;" + ("display:none;" if hidden else "")
        return f'<iframe src="{html.escape(self.url, quote=True)}" style="{style}"></iframe>'

    def _repr_html_(self): return self.to_html()
    def __repr__(self): return f"<Project {'/'.join(self.path)}>"
