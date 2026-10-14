"""Serialize the public manifest-entry shape with Pydantic's own option semantics.

Architecture: immutable native entry -> public-field projection -> Pydantic
Python/JSON serializer. File storage, checksums and parent handles never enter
this projection. Construction/validation and serialization are separate stages:
serializing a saved entry must not reopen its original local file.
Main callers: ArtifactManifestEntry.model_dump and model_dump_json.
Why this exists: upstream exposes Pydantic's nested filtering, custom serializers
and context options. Reuse that runtime rather than approximate its argument
branches or invent a second implementation of its serialization rules.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import SchemaSerializer, core_schema

from .artifact_entry_fields import LogicalPath


class _PublicEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True, validate_by_name=True,
                              validate_by_alias=True, serialize_by_alias=True,
                              arbitrary_types_allowed=True)
    path: LogicalPath
    digest: str
    ref: str | None = None
    birth_artifact_id: str | None = Field(default=None, alias="birthArtifactID")
    size: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    local_path: str | None = None
    skip_cache: bool = False


def projection(entry):
    """Project already validated values while preserving explicit-field tracking.

    Main callers: both public serializers. model_construct deliberately skips
    construction validators: serialization cannot require a still-existing
    source file or alter an immutable manifest's inferred size.
    """
    values = {name: getattr(entry, name) for name in _PublicEntry.model_fields}
    return _PublicEntry.model_construct(_fields_set=set(entry._public_fields_set), **values)


def _serialize_nested_entry(value, handler, info):
    """Use the public projection when an entry appears inside another value.

    Main callers: Pydantic Any-field serialization. Without this hook the native
    dataclass serializer would expose parent handles and storage-only fields.
    Delegating to the existing handler retains context and nested filters.
    """
    return handler(projection(value))


public_entry_serializer = SchemaSerializer(core_schema.any_schema(
    serialization=core_schema.wrap_serializer_function_ser_schema(
        _serialize_nested_entry, info_arg=True)))


def public_entry_schema(entry_type):
    """Keep annotated nested entries on the same public serialization boundary.

    Main callers: Pydantic schema generation for a user model with an entry-typed
    field. Any-field dispatch uses public_entry_serializer; annotated dataclass
    fields otherwise generate a separate serializer that includes private state.
    This hook keeps both nesting forms consistent for existing entry instances.
    """
    return core_schema.is_instance_schema(entry_type,
        serialization=core_schema.wrap_serializer_function_ser_schema(
            _serialize_nested_entry, info_arg=True))
