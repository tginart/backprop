"""Atomic native wrappers around the pinned pure dtype machinery.

Architecture: public dtype -> isolated Type instance -> exact assign/refinement
-> ColumnType view -> declarative native schema. Reads consult only the explicit
TypeRegistry; logged data never imports Python modules. Custom handlers keep
their actual local validation and require explicit registration to reconstruct.
Main callers: ColumnType.from_dtype/from_json/assign/to_json.
Why this exists: inference mutates schemas over time, while a rejected Table row
must leave every previously committed column constraint untouched.
"""
from __future__ import annotations

import copy

from . import table_types as types
from .table_native_types import NATIVE_TYPES
from .table_image_type import _ImageFileType
from .table_schema_values import encode_value, decode_value


_UPSTREAM_MODULE = "wandb.sdk.data_types._dtypes"
_PARAMS = {"invalid": set(), "any": set(), "unknown": set(), "none": set(),
           "string": set(), "number": set(), "timestamp": set(), "boolean": set(),
           "const": {"val", "is_set"}, "union": {"allowed_types"},
           "list": {"element_type", "length"}, "typedDict": {"type_map"},
           "pythonObject": {"class_name"}, "ndarray": {"shape", "serialization_path"}}
# A user may register custom types before the first Table imports this module.
# Resolve the baseline from actual vendored class definitions, not the mutable
# live registry, so those registrations remain custom rather than "standard".
_STANDARD_HANDLERS = {
    name: handler
    for handler in [*vars(types).values(), *NATIVE_TYPES, _ImageFileType]
    if isinstance(handler, type) and issubclass(handler, types.Type) and handler.name
    for name in [handler.name, *handler.legacy_names]
}


def _is_type(cls):
    return any(base.__name__ == "Type" and base.__module__ in {_UPSTREAM_MODULE, types.__name__}
               for base in cls.__mro__)


def from_upstream_dtype(dtype, optional):
    """Accept standard type classes/instances and preserve custom local handlers."""
    cls = dtype if isinstance(dtype, type) else type(dtype)
    if not _is_type(cls):
        return None
    instance = dtype() if isinstance(dtype, type) else dtype
    from .foreign_type_family import family_of, contains_custom
    family = family_of(instance)
    if family is not None and contains_custom(instance, family):
        instance = _clone(instance)
        return _wrap(family.optional(instance) if optional else instance, optional)
    standard = _STANDARD_HANDLERS.get(instance.name)
    if (standard is not None and cls is not standard
            and family is not None and family.standard(instance)):
        # Standard instances from an already imported official SDK use its Type
        # identity internally. Rebuild their declarations with our identical
        # pure classes before assigning native Python values.
        instance = _restore(_capture(instance))
    instance = _clone(instance)
    return _wrap(types.OptionalType(instance) if optional and isinstance(instance, types.Type) else instance, optional)


def from_public_dtype(dtype, optional):
    """Resolve plain Python dtype declarations through the pinned registry."""
    from .foreign_type_family import public_family
    family = public_family(dtype)
    if family is not None:
        instance = _clone(family.registry.type_from_dtype(dtype))
        return _wrap(family.optional(instance) if optional else instance, optional)
    instance = types.TypeRegistry.type_from_dtype(dtype)
    return _wrap(types.OptionalType(instance) if optional else instance, optional)


def _clone(instance):
    # User constants and relationship targets are data, not mutable type state.
    # Keep their identity while isolating the type's own evolving parameters.
    memo = {}
    visited = set()
    def preserve(value):
        if id(value) in visited:
            return
        visited.add(id(value))
        if _is_type(type(value)):
            cls = type(value)
            if cls is types.ConstType or cls.__module__ == _UPSTREAM_MODULE and cls.__name__ == "ConstType":
                # ConstType's exact Python value identity is meaningful (for
                # example a NumPy array compared with itself). It is declared
                # data, not custom handler state; preserve only this boundary.
                constant = value.params["val"]
                memo[id(constant)] = constant
            preserve(value.params)
            # Native media handlers may retain an immutable WBValue reference
            # outside params (ClassesIdType does this for its class metadata).
            for key, item in vars(value).items():
                if key != "_params":
                    preserve(item)
        elif isinstance(value, dict):
            for item in value.values(): preserve(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value: preserve(item)
        elif hasattr(value, "_log_type"):
            memo[id(value)] = value
    preserve(instance)
    return copy.deepcopy(instance, memo)


def _wrap(instance, optional=False):
    """Expose useful native type metadata while retaining the complete engine."""
    from .tables import ColumnType
    view = instance
    nullable = optional
    if view.name == "union":
        children = view.params["allowed_types"]
        nonnull = [child for child in children if child.name != "none"]
        if len(nonnull) == 1 and len(nonnull) != len(children):
            nullable = True
            # Upstream key wrapping applies only to a direct key type. A
            # nullable union containing a key remains a union and permits
            # ordinary scalars/None without creating TableKey/TableIndex data.
            if nonnull[0].name not in {"primaryKey", "foreignKey", "foreignIndex"}:
                view = nonnull[0]
    params = view.params
    return ColumnType("dictionary" if view.name == "typedDict" else view.name,
                      nullable, item_type=_wrap(params["element_type"]) if view.name == "list" else None,
                      fields={key: _wrap(child) for key, child in params["type_map"].items()} if view.name == "typedDict" else
                      {key: _wrap(child) for key, child in params["column_types"].params["type_map"].items()} if view.name == "table" and params["column_types"].name == "typedDict" else None,
                      shape=params.get("shape") if view.name == "ndarray" else None,
                      options=tuple(_wrap(child) for child in params["allowed_types"]) if view.name == "union" else None,
                      constant=params.get("val") if view.name == "const" else None,
                      table=params.get("table") if view.name in {"foreignKey", "foreignIndex"} else None,
                      column=params.get("col_name") if view.name == "foreignKey" else None,
                      class_set=view.wb_classes_obj_ref._class_set if view.name == "classesId" else None,
                      _runtime_type=instance)


def assign(column, value):
    """Refine an isolated type and return it only after exact validation succeeds."""
    instance = _clone(column._runtime_type)
    result = instance.assign(value)
    if result.name == "invalid":
        raise TypeError(f"Value {value!r} does not match Table dtype {instance!r}")
    return _wrap(result, column.optional)


def _capture(instance, artifact=None, native_tables=None):
    """Capture exact constraints, retaining constant container/number identity."""
    from .foreign_type_adapter import original_type
    instance = original_type(instance)
    name, params = instance.name, instance.params
    native_tables = {} if native_tables is None else native_tables
    from .table_native_schema import capture_reference
    reference = capture_reference(instance, artifact, native_tables)
    if reference is not None:
        return reference
    standard = _STANDARD_HANDLERS.get(name)
    if standard is not None and type(instance) is not standard and not (
            type(instance).__module__.startswith("wandb.sdk.data_types.") and type(instance).__name__ == standard.__name__):
        raise TypeError(f"Custom Table dtype {name!r} must declare a unique name instead of replacing a standard type")
    if name == "const":
        return {"wb_type": name, "params": {"val": encode_value(params["val"]), "is_set": params["is_set"]}, "native_constants": True}
    if name == "list":
        return {"wb_type": name, "params": {"element_type": _capture(params["element_type"], artifact, native_tables), "length": params["length"]}}
    if name == "typedDict":
        return {"wb_type": name, "params": {"type_map": {key: _capture(value, artifact, native_tables) for key, value in params["type_map"].items()}}}
    if name == "union":
        return {"wb_type": name, "params": {"allowed_types": [_capture(value, artifact, native_tables) for value in params["allowed_types"]]}}
    if name == "table":
        return {"wb_type": name, "params": {"column_types": _capture(params["column_types"], artifact, native_tables)}}
    return copy.deepcopy(instance.to_json(artifact))


def to_schema(column, artifact=None):
    """Persist only reconstructible types, never an arbitrary callable or pickle."""
    instance = column._runtime_type
    from .foreign_type_family import family_of, registered_handler
    family = family_of(instance)
    registered, _ = registered_handler(instance.name, types.TypeRegistry.types_by_name(),
        preferred=family, standard_names=_STANDARD_HANDLERS)
    if registered is not type(instance):
        raise TypeError(f"Table dtype {instance.name!r} needs its exact handler registered with TypeRegistry.add before logging")
    native_tables = {}
    schema = _capture(instance, artifact, native_tables)
    _restore(schema, artifact, native_tables, family=family)  # Reject discarded constraints before a journal is changed.
    return schema


def from_schema(schema, optional=False, artifact=None):
    """Reconstruct only declared types already registered by trusted local code."""
    instance = _restore(copy.deepcopy(schema), artifact)
    # Older native scalar descriptors stored optionality outside the schema.
    # New schemas already contain it; never grow another None alternative on
    # each read or assignment.
    if optional and _clone(instance).assign(None).name == "invalid":
        from .foreign_type_family import family_of
        family = family_of(instance)
        instance = family.optional(instance) if family else types.OptionalType(instance)
    return _wrap(instance, optional)


def _restore(schema, artifact=None, native_tables=None, *, family=None, native_columns=False):
    if not isinstance(schema, dict):
        raise TypeError("Invalid upstream Table dtype schema")
    name, params = schema.get("wb_type"), schema.get("params", {})
    native_tables = {} if native_tables is None else native_tables
    if not isinstance(params, dict):
        raise TypeError("Upstream Table dtype params must be a dictionary")
    from .foreign_type_family import schema_family, registered_handler
    handlers = types.TypeRegistry.types_by_name()
    family = None if native_columns else family or schema_family(schema, handlers, _STANDARD_HANDLERS)
    handler, _ = registered_handler(name, handlers, preferred=family, standard_names=_STANDARD_HANDLERS)
    if handler is None:
        raise TypeError(f"Table dtype {name!r} has no explicitly registered handler")
    if family is not None and not issubclass(handler, family.base):
        raise TypeError(f"Table dtype {name!r} has an incompatible registered Type family")
    from .table_native_schema import restore_reference
    reference = restore_reference(schema, artifact, native_tables)
    if reference is not None:
        return reference
    expected = _STANDARD_HANDLERS.get(name)
    if family is not None and expected is not None:
        expected = family.namespace.get(expected.__name__)
    if name in _STANDARD_HANDLERS and handler is not expected:
        raise TypeError(f"Custom Table dtype {name!r} must declare a unique name instead of replacing a standard type")
    # Legacy dictionary/object aliases name the same trusted handlers. Validate
    # and recurse using their canonical names, retaining the original payload
    # for custom from_json methods which may intentionally inspect an alias.
    name = expected.name if expected is not None else name
    pure = handler.__module__ in {types.__name__, _UPSTREAM_MODULE}
    if pure and set(schema) - {"wb_type", "params", "native_constants"}:
        raise TypeError("Invalid upstream Table dtype schema")
    if pure and set(params) - _PARAMS.get(name, set()):
        raise TypeError(f"Unsupported upstream Table dtype schema: {name!r}; constraints cannot be discarded")
    if name == "const" and handler is expected:
        value = decode_value(params.get("val")) if schema.get("native_constants") else params.get("val")
        return handler(value, is_set=params.get("is_set", False))
    if name == "union" and handler is expected:
        return handler([_restore(child, artifact, native_tables, family=family) for child in params.get("allowed_types", [])])
    if name == "list" and handler is expected:
        return handler(_restore(params["element_type"], artifact, native_tables, family=family) if "element_type" in params else None, params.get("length"))
    if name == "typedDict" and handler is expected:
        if native_columns:
            from .foreign_type_adapter import native_child
            # Validate the ordinary parent schema above, then restore each
            # native Table column in its own explicitly registered family.
            return handler({key: native_child(_restore(child, artifact, native_tables))
                            for key, child in params.get("type_map", {}).items()})
        return handler({key: _restore(child, artifact, native_tables, family=family) for key, child in params.get("type_map", {}).items()})
    if name == "table" and handler.__module__ == "backprop.table_native_types":
        return handler(_restore(params["column_types"], artifact, native_tables, native_columns=True))
    return handler.from_json(schema, artifact)


def needs_artifact_owner(column_schema):
    """Identify real custom/media/reference codecs in a saved column schema.

    Main callers: implicit_artifacts.restore_table_types. Const values and
    arbitrary custom params are data; a wb_type key there cannot trigger a
    source-artifact lookup or select executable code.
    """
    from .foreign_type_family import schema_handlers
    if not isinstance(column_schema, dict):
        return False
    if "upstream_schema" in column_schema:
        return any(handler is not None and handler.__module__ not in {types.__name__, _UPSTREAM_MODULE}
                   for handler, _ in schema_handlers(column_schema["upstream_schema"],
                       types.TypeRegistry.types_by_name(), _STANDARD_HANDLERS))
    kind = column_schema.get("wb_type")
    if kind in {"foreignKey", "foreignIndex"}:
        return True
    if kind == "list":
        return needs_artifact_owner(column_schema.get("item_type"))
    if kind == "dictionary":
        return any(needs_artifact_owner(child) for child in column_schema.get("fields", {}).values())
    if kind == "union":
        return any(needs_artifact_owner(child) for child in column_schema.get("options", []))
    return False
