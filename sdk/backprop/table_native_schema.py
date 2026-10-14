"""Separate native Table references from public WBType serialization.

Architecture: actual WBType and Table identity -> declarative schema plus native
history navigation IDs -> artifact entry paths when an owner exists -> resolved
Table objects on read. Public adapter to_json/from_json remains source-shaped.
Main callers: table_dtype_bridge and legacy ColumnType decoding.
Why this exists: a static history viewer needs IDs, while an immutable artifact
needs self-contained referenced entries. Neither representation embeds Python.
"""
from __future__ import annotations

from . import table_types as types


def capture_reference(instance, artifact, tables):
    """Capture a native relationship and preserve its actual local target."""
    from .table_native_types import _ForeignKeyType, _ForeignIndexType
    if not isinstance(instance, (_ForeignKeyType, _ForeignIndexType)):
        return None
    target = instance.params["table"]
    tables[target._id] = target
    params = {"table": target._id}
    mode = "id"
    if artifact is not None:
        name = f"media/tables/ref-{artifact._reference_counter}"
        artifact._reference_counter += 1
        params["table"] = artifact.add(target, name).path
        mode = "path"
    if isinstance(instance, _ForeignKeyType):
        params["col_name"] = instance.params["col_name"]
    return {"wb_type": instance.name, "params": params, "native_table_ref": mode}


def restore_reference(schema, artifact, tables):
    """Resolve only known native references; absent targets fail explicitly."""
    from .table_native_types import _ForeignKeyType, _ForeignIndexType
    mode = schema.get("native_table_ref")
    if mode is None:
        return None
    name, params = schema.get("wb_type"), schema.get("params", {})
    if name not in {"foreignKey", "foreignIndex"} or mode not in {"id", "path"}:
        raise TypeError("Invalid native Table reference schema")
    if mode == "id":
        target = tables.get(params.get("table"))
        if target is None:
            raise ValueError("A native Table ID requires its original table context")
    else:
        if artifact is None:
            raise ValueError("A stored Table reference requires its source artifact")
        target = artifact.get(params["table"])
        if target is None:
            raise ValueError("The referenced Table is unavailable")
    return _ForeignKeyType(target, params["col_name"]) if name == "foreignKey" else _ForeignIndexType(target)


def legacy_runtime(column):
    """Translate stored native schema wrappers to actual WBType instances.

    Main callers: ColumnType.from_dtype and legacy table reads. New public dtype
    inputs always use TypeRegistry; this only interprets existing storage tags.
    """
    from .table_native_types import _PrimaryKeyType, _ForeignKeyType, _ForeignIndexType, _ClassesIdType
    from .media import Classes
    if column._runtime_type is not None:
        return column._runtime_type
    kind = column.kind
    if kind == "primaryKey":
        result = _PrimaryKeyType()
    elif kind == "foreignKey":
        result = _ForeignKeyType(column.table, column.column)
    elif kind == "foreignIndex":
        result = _ForeignIndexType(column.table)
    elif kind == "classesId":
        result = _ClassesIdType(Classes(column.class_set))
    elif kind == "const":
        result = types.ConstType(column.constant)
    elif kind == "list":
        result = types.ListType(legacy_runtime(column.item_type) if column.item_type else None)
    elif kind == "dictionary":
        result = types.TypedDictType({key: legacy_runtime(value) for key, value in (column.fields or {}).items()})
    elif kind == "union":
        result = types.UnionType([legacy_runtime(value) for value in column.options])
    elif kind == "ndarray":
        result = types.NDArrayType(column.shape)
    elif isinstance(kind, type):
        # Historical native class tags described the value's class, rather
        # than a new public dtype=SomeClass declaration.
        handler = types.TypeRegistry.types_by_class().get(kind)
        result = handler() if handler is not None else types.PythonObjectType(kind.__name__)
    else:
        handler = types.TypeRegistry.types_by_name().get(kind)
        if handler is None:
            raise TypeError(f"Unknown native Table dtype: {kind}")
        result = handler()
    return types.OptionalType(result) if column.optional else result


def navigation_fields(column, schema=None, *, use_paths=False):
    """Describe nested viewer relationships without serializing types again.

    Main callers: ColumnType.to_json after its complete schema is captured.
    Why this exists: custom type codecs must run once with their real artifact
    owner; the parallel viewer metadata only reads already-refined constraints.
    Artifact navigation takes paths from that captured schema, while history
    navigation retains native Table IDs from the original in-memory objects.
    """
    schema = schema or {}
    if schema.get("wb_type") == "union" and column.kind != "union":
        name = "typedDict" if column.kind == "dictionary" else column.kind
        schema = next((child for child in schema.get("params", {}).get("allowed_types", [])
                       if child.get("wb_type") == name), {})
    params = schema.get("params", {})

    def view(child, captured):
        result = {"wb_type": child.kind, "optional": child.optional}
        if child.table is not None:
            result["table_id"] = child.table._id
            if child.column is not None:
                result["column"] = child.column
            if use_paths and captured.get("native_table_ref") == "path":
                result["table_path"] = captured["params"]["table"]
        if child.class_set is not None:
            result["class_set"] = child.class_set
        result.update(navigation_fields(child, captured, use_paths=use_paths))
        return result

    result = {}
    if column.fields is not None:
        field = "column_types" if column.kind == "table" else "fields"
        children = params.get("column_types", {}).get("params", {}).get("type_map", {}) if field == "column_types" else params.get("type_map", {})
        result[field] = {key: view(child, children.get(key, {})) for key, child in column.fields.items()}
    if column.item_type is not None:
        result["item_type"] = view(column.item_type, params.get("element_type", {}))
    if column.options is not None:
        children = params.get("allowed_types", [])
        result["options"] = [view(child, children[index] if index < len(children) else {})
                             for index, child in enumerate(column.options)]
    return result


def relationship_targets(columns):
    """Yield each referenced native Table once, including nested constraints.

    Main callers: Encoder's related_tables snapshot collection. This walks type
    declarations, not target data. Encoder owns Table cycle detection and the
    rule that related targets do not advance independent logging cursors.
    """
    from .table_native_types import _ForeignKeyType, _ForeignIndexType
    seen_types, seen_tables = set(), set()
    def visit(value):
        if id(value) in seen_types:
            return
        seen_types.add(id(value))
        if isinstance(value, (_ForeignKeyType, _ForeignIndexType)):
            target = value.params["table"]
            if id(target) not in seen_tables:
                seen_tables.add(id(target))
                yield target
        elif isinstance(value, types.Type):
            yield from visit(value.params)
        elif isinstance(value, dict):
            for child in value.values():
                yield from visit(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                yield from visit(child)
    for column in columns:
        yield from visit(legacy_runtime(column))
