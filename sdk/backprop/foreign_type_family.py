"""Trusted, already-loaded W&B dtype families at the native Table boundary.

Architecture: a supplied Type reveals its existing Python globals -> remember
that family's TypeRegistry -> retain its actual validation/container classes ->
resolve saved custom schemas only through explicit current registrations.
Main callers: table_dtype_bridge. This never imports or executes a module named
by logged data. Keeping families separate prevents native List/Union constructors
from mistaking a foreign Type instance for a literal constant.
"""
from __future__ import annotations

from dataclasses import dataclass
import sys
import weakref

MODULE = "wandb.sdk.data_types._dtypes"
_families = []


@dataclass(eq=False)
class TypeFamily:
    _base: weakref.ReferenceType
    _registry: weakref.ReferenceType

    @property
    def base(self):
        return self._base()

    @property
    def registry(self):
        return self._registry()

    @property
    def namespace(self):
        base = self.base
        return getattr(base.__dict__.get("assign"), "__globals__", {}) if base else {}

    def standard(self, value):
        """Recognize an original class by identity in its defining globals."""
        cls = value if isinstance(value, type) else type(value)
        return cls.__module__ == MODULE and self.namespace.get(cls.__name__) is cls

    def handler(self, name):
        registry = self.registry
        return registry.types_by_name().get(name) if registry else None

    def optional(self, instance):
        return self.namespace["OptionalType"](instance)


def family_of(value):
    """Learn a family only from an actual supplied foreign Type class/instance.

    Main callers: dtype conversion and schema writing. A module name by itself
    is insufficient: Type.assign must refer back to this exact Type and registry.
    Trusted caller Python can supply custom executable validation, as upstream
    permits; a stored dictionary cannot manufacture that trust boundary.
    """
    cls = value if isinstance(value, type) else type(value)
    for base in cls.__mro__:
        if base.__name__ != "Type" or base.__module__ != MODULE:
            continue
        namespace = getattr(base.__dict__.get("assign"), "__globals__", {})
        registry = namespace.get("TypeRegistry")
        if namespace.get("Type") is not base or not isinstance(registry, type):
            continue
        if getattr(registry.type_from_dtype, "__globals__", {}).get("Type") is not base:
            continue
        _families[:] = [family for family in _families if family.base is not None and family.registry is not None]
        for family in _families:
            if family.base is base:
                return family
        # The table/value or already-imported module owns executable code.
        # This index must not keep a removed SDK module or temporary test
        # family alive merely because it once supplied a dtype.
        family = TypeFamily(weakref.ref(base), weakref.ref(registry))
        _families.append(family)
        return family
    return None


def public_family(dtype):
    """Find one foreign family in public list/dictionary dtype declarations."""
    families = []
    def visit(value):
        family = family_of(value)
        if family is not None:
            if family not in families:
                families.append(family)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(dtype)
    if len(families) > 1:
        raise TypeError("A Table dtype declaration contains incompatible W&B Type families")
    return families[0] if families else None


def contains_custom(instance, family):
    """Keep custom branches in their original engine, including nested params."""
    seen = set()
    def visit(value):
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, family.base):
            return not family.standard(value) or visit(value.params)
        if isinstance(value, dict):
            return any(visit(item) for item in value.values())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(visit(item) for item in value)
        return False
    return visit(instance)


def registered_handler(name, native_handlers, *, preferred=None, standard_names=()):
    """Resolve only a unique explicit registration, never arbitrary module text.

    Main callers: schema reads. A fresh process may already have imported and
    registered its official SDK handler before importing Backprop; inspect that
    exact loaded module without importing it. Collisions cannot choose which
    local executable handler interprets a saved constraint silently.
    """
    module = sys.modules.get(MODULE)
    if module is not None and isinstance(getattr(module, "Type", None), type):
        family_of(module.Type)
    native = native_handlers.get(name)
    if name in standard_names:
        return (preferred.handler(name), preferred) if preferred else (native, None)
    candidates = [(native, None)] if native is not None else []
    for family in _families:
        handler = family.handler(name)
        if handler is not None:
            candidates.append((handler, family))
    distinct = {id(handler) for handler, _ in candidates}
    if len(distinct) > 1:
        raise TypeError(f"Table dtype {name!r} has ambiguous explicitly registered handlers")
    if preferred is not None:
        handler = preferred.handler(name)
        if handler is not None and candidates and candidates[0][0] is handler:
            return handler, preferred
    return candidates[0] if candidates else (None, None)


def schema_handlers(schema, native_handlers, standard_handlers, *, descend_tables=True):
    """Walk declared schema children, never constants or custom parameter data.

    Main callers: family selection and resumed artifact-owner discovery. Aliases
    resolve through the trusted registry before deciding structural child paths.
    A custom codec owns its payload, so only that codec decides how to decode it.
    """
    if not isinstance(schema, dict):
        return
    name = schema.get("wb_type")
    if not isinstance(name, str):
        return
    handler, family = registered_handler(name, native_handlers, standard_names=standard_handlers)
    yield handler, family
    expected = standard_handlers.get(name)
    if expected is None or handler is not expected:
        return
    name = expected.name
    params = schema.get("params", {})
    if not isinstance(params, dict):
        return
    children = []
    if name == "list":
        children = [params.get("element_type")]
    elif name == "union" and isinstance(params.get("allowed_types", []), list):
        children = params.get("allowed_types", [])
    elif name == "typedDict" and isinstance(params.get("type_map", {}), dict):
        children = params.get("type_map", {}).values()
    elif name == "table" and descend_tables:
        children = [params.get("column_types")]
    for child in children:
        yield from schema_handlers(child, native_handlers, standard_handlers, descend_tables=descend_tables)


def schema_family(schema, native_handlers, standard_names):
    """Choose the already-registered custom family needed by a schema tree."""
    families = []
    # Native Table owns its own column engine. Its foreign children are adapted
    # separately, rather than moving the enclosing native Table into that SDK.
    for _, family in schema_handlers(schema, native_handlers, standard_names, descend_tables=False):
        if family is not None and family not in families:
            families.append(family)
    if len(families) > 1:
        raise TypeError("Stored Table dtype has incompatible W&B Type families")
    return families[0] if families else None
