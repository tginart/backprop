"""Match pinned Table JSON keys while retaining resumed Python constraints.

Architecture: Table cells and public schemas use sorted JSON object keys;
history alone carries a declarative copy of a schema when that conversion loses
Python key identity. Resumed writers restore that copy, while Artifact.get and
public history see the same string keys as pinned json.dump(sort_keys=True).
No user object is mutated and no logged descriptor imports or executes code.
Main callers: Encoder, ObjectEncoder and restore_table_types.
"""
from __future__ import annotations

import json


def encode_mapping(value, encode):
    """Apply the actual JSON key conversion after pinned key-order validation.

    Main callers: Table cell encoding and schema normalization. Sorting before
    conversion matters: pinned artifact JSON rejects incomparable mixed keys,
    such as strings beside integers. A plain str(key) would also spell bool,
    None and nonfinite float keys incorrectly.
    """
    result = {}
    for key, item in sorted(value.items()):
        name = str(key) if isinstance(key, str) else next(iter(json.loads(json.dumps({key: None}))))
        result[name] = encode(item)
    return result


def _constant_keys(value):
    """Normalize only dictionary keys inside an already decoded constant."""
    if isinstance(value, dict):
        return encode_mapping(value, _constant_keys)
    if type(value) in {list, tuple, set, frozenset}:
        return type(value)(_constant_keys(item) for item in value)
    return value


def _has_python_keys(value):
    if isinstance(value, dict):
        return any(not isinstance(key, str) or _has_python_keys(item) for key, item in value.items())
    if type(value) in {list, tuple, set, frozenset}:
        return any(_has_python_keys(item) for item in value)
    return False


def _normalize_schema(value):
    """Normalize raw schema objects and dictionaries inside tagged constants."""
    if isinstance(value, dict):
        if value.get("wb_type") == "const" and value.get("native_constants") is True:
            from .table_schema_values import decode_value, encode_value
            constant = decode_value(value["params"]["val"])
            value = {**value, "params": {**value["params"], "val": encode_value(_constant_keys(constant))}}
        return encode_mapping(value, _normalize_schema)
    if isinstance(value, (list, tuple)):
        return [_normalize_schema(item) for item in value]
    return value


def _needs_resume_keys(value):
    if isinstance(value, dict):
        if value.get("wb_type") == "const" and value.get("native_constants") is True:
            from .table_schema_values import decode_value
            if _has_python_keys(decode_value(value["params"]["val"])):
                return True
        return any(not isinstance(key, str) or _needs_resume_keys(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_needs_resume_keys(item) for item in value)
    return False


def table_schema_fields(schemas, *, for_resume=False):
    """Produce public JSON schemas and, only when needed, writer resume state.

    Main callers: history and immutable artifact Table encoders. Why this exists:
    a stored integer dictionary key becomes a string, but a resumed training
    process must still be able to append its original integer-keyed Python data.
    The extra field contains only the existing declarative schema, encoded with
    fixed scalar/container tags. Artifact contents never receive that field.
    """
    result = {"column_types": _normalize_schema(schemas)}
    if for_resume and _needs_resume_keys(schemas):
        from .table_schema_values import encode_value
        result["resume_column_types"] = encode_value(schemas)
    return result


def resume_schemas(descriptor):
    """Recover declared writer constraints without interpreting Python code."""
    if "resume_column_types" not in descriptor:
        return descriptor.get("column_types", [])
    from .table_schema_values import decode_value
    schemas = decode_value(descriptor["resume_column_types"])
    if not isinstance(schemas, list) or any(not isinstance(item, dict) for item in schemas):
        raise ValueError("Stored resume Table schemas must be a list of declarations")
    return schemas
