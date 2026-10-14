"""Keep imported custom column engines inside native Table-of-Table schemas.

Architecture: Table._column_types wraps only foreign children -> unchanged native
TypedDict/TableType algorithms delegate validation -> storage unwraps the original
handler -> readback restores and wraps each column independently. Direct public
Table.cast still returns the original foreign type, not this internal adapter.
Main callers: Table._column_types and table_dtype_bridge's native Table reader.
Why this exists: native TypeRegistry otherwise treats an imported Type object as
constant data when a supported Table becomes a cell inside another Table.
"""
from __future__ import annotations

from . import table_types as native
from .foreign_type_family import family_of


def native_child(value):
    """Adapt an actual foreign Type for a native structural engine boundary."""
    if isinstance(value, native.Type):
        return value
    family = family_of(value)
    if family is None:
        raise TypeError("A Table column schema must contain an actual registered Type")
    return native.InvalidType() if value.name == "invalid" else _NativeForeignType(value)


def original_type(value):
    """Recover the actual codec before declarative schema capture."""
    return value._foreign if isinstance(value, _NativeForeignType) else value


class _NativeForeignType(native.Type):
    """Delegate actual foreign validation without making it a native constant.

    Main callers: native TypedDictType.assign/assign_type while validating a
    nested Table. No registration or handler import is performed by this class.
    Native primitive schemas are translated only at this internal comparison
    boundary, where both sides already came from actual Table column engines.
    """
    def __init__(self, foreign):
        self._foreign = foreign

    @property
    def name(self):
        return self._foreign.name

    @property
    def params(self):
        return self._foreign.params

    def assign(self, value=None):
        return native_child(self._foreign.assign(value))

    def assign_type(self, other=None):
        other = original_type(other)
        if isinstance(other, native.Type):
            from .table_dtype_bridge import _capture, _restore
            family = family_of(self._foreign)
            # Only pure primitives/containers can cross without a media owner.
            # A foreign custom handler cannot interpret a different native
            # custom handler's executable type identity by name alone.
            if type(other).__module__ != native.__name__:
                return native.InvalidType()
            other = _restore(_capture(other), family=family)
        return native_child(self._foreign.assign_type(other))

    def to_json(self, artifact=None):
        return self._foreign.to_json(artifact)
