"""Portable artifact lookup paths before Foundry event selection.

Architecture: separate selector from collection path -> validate typed path
parts -> fill missing namespace defaults -> require a concrete lookup scope.
Main caller: Api.artifact. Selectors may contain slashes even though current
alias creation rejects them; parsing must never treat a selector as a namespace.
"""
from dataclasses import replace
from typing import Optional
from pydantic.dataclasses import dataclass


@dataclass
class ArtifactPath:
    name: str
    project: Optional[str] = None
    prefix: Optional[str] = None


@dataclass
class FullArtifactPath(ArtifactPath):
    name: str
    project: str
    prefix: str


def lookup_path(name, *, entity, project):
    """Resolve one-, two- or three-part paths with pinned default precedence.

    Main caller: Api.artifact. Foundry's empty entity namespace is retained as
    an explicit string; a missing entity is distinct and must be supplied.
    """
    if name is None:
        raise ValueError("You must specify name= to fetch an artifact.")
    collection, separator, selector = name.partition(':')
    parts = collection.split('/')
    if len(parts) > 3:
        raise ValueError(f"Invalid artifact path: {name!r}")
    fields = dict(zip(('name', 'project', 'prefix'), reversed(parts)))
    fields['name'] += separator + selector
    parsed = ArtifactPath(**fields)
    parsed = replace(parsed, prefix=parsed.prefix or entity, project=parsed.project or project or 'uncategorized')
    if parsed.prefix is None:
        raise ValueError("Could not determine entity. Please include the entity as part of the artifact name path.")
    return FullArtifactPath(name=parsed.name, project=parsed.project, prefix=parsed.prefix)
