"""Typed tables with atomic mutation and explicit logging generations.

Architecture: ColumnType describes accepted values; each mutation validates a
candidate schema/data set before swapping it into the Table; Encoder snapshots
the table and advances logging cursors only after the event journal commits.
Optional scientific libraries are imported only for conversion requests.
Main callers: training scripts, chart helpers, and data_types.Encoder.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import importlib
import uuid
from .media_base import Media


def _optional_module(name, purpose):
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise ImportError(f"{purpose} requires the optional {name} package in your Python environment") from error


@dataclass(frozen=True)
class ColumnType:
    """Internal schema/navigation view around the actual returned WBType."""

    kind: object = "unknown"
    optional: bool = True
    item_type: object = None
    fields: object = None
    table: object = None
    column: object = None
    class_set: object = None
    shape: object = None
    options: object = None
    constant: object = None
    upstream_schema: object = None
    _runtime_type: object = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dtype(cls, dtype, optional=False):
        """Interpret public dtype values separately from persisted schema tags.

        Main callers: Table construction and cast. A concrete string such as
        'int' is a constant, never shorthand for the Python int class. Only the
        constructor's whole dtype=None argument requests inference; a per-column
        None or cast(None) constrains that column to the literal None value.
        """
        from .table_dtype_bridge import from_upstream_dtype, from_public_dtype
        if isinstance(dtype, ColumnType):
            from .table_native_schema import legacy_runtime
            runtime = legacy_runtime(dtype)
            copied = from_upstream_dtype(runtime, optional and not dtype.optional)
            return replace(copied, optional=optional or dtype.optional)
        upstream = from_upstream_dtype(dtype, optional)
        return upstream if upstream is not None else from_public_dtype(dtype, optional)

    def assign(self, value):
        """Return the compatible inferred type; leave this immutable type intact."""
        from .table_dtype_bridge import assign, from_upstream_dtype
        if self._runtime_type is not None:
            return assign(self, value)
        from .table_native_schema import legacy_runtime
        return assign(from_upstream_dtype(legacy_runtime(self), False), value)

    def to_json(self, artifact=None, *, _relationship_ids=False):
        """Serialize constraints with the real owner when one is available.

        Main callers: Encoder history descriptors and immutable ObjectEncoder.
        History descriptors keep native Table IDs for linked-row navigation;
        artifact object files replace those IDs with copied table entry paths.
        Both stages still pass the owner to custom dtype serialization.
        """
        kind = self.kind if isinstance(self.kind, str) else self.kind.__name__
        result = {"wb_type": kind, "optional": self.optional}
        if self._runtime_type is not None:
            from .table_dtype_bridge import to_schema
            result["upstream_schema"] = to_schema(self, artifact)
            from .table_native_schema import navigation_fields
            result.update(navigation_fields(self, result["upstream_schema"], use_paths=artifact is not None and not _relationship_ids))
            if self.table is not None:
                result["table_id"] = self.table._id
                if self.column is not None:
                    result["column"] = self.column
                if artifact is not None and not _relationship_ids:
                    result["table_path"] = result["upstream_schema"]["params"]["table"]
            if self.class_set is not None:
                result["class_set"] = self.class_set
            return result
        if self.upstream_schema is not None:
            from copy import deepcopy
            result["upstream_schema"] = deepcopy(self.upstream_schema)
            return result
        if self.item_type:
            result["item_type"] = self.item_type.to_json(artifact, _relationship_ids=_relationship_ids)
        if self.fields is not None:
            result["fields"] = {name: typ.to_json(artifact, _relationship_ids=_relationship_ids) for name, typ in self.fields.items()}
        if self.class_set is not None:
            result["class_set"] = self.class_set
        if self.shape is not None:
            result["shape"] = self.shape
        if self.options is not None:
            result["options"] = [item.to_json(artifact, _relationship_ids=_relationship_ids) for item in self.options]
        if self.kind == "const":
            result["constant"] = self.constant
        if self.kind in {"foreignKey", "foreignIndex"}:
            result["table_id"] = self.table._id
            if self.column is not None:
                result["column"] = self.column
            if artifact is not None and not _relationship_ids:
                name = f"media/tables/ref-{artifact._reference_counter}"
                artifact._reference_counter += 1
                result["table_path"] = artifact.add(self.table, name).path
                del result["table_id"]
        return result

    @classmethod
    def from_json(cls, value, artifact=None):
        if "upstream_schema" in value:
            from .table_dtype_bridge import from_schema
            return from_schema(value["upstream_schema"], value.get("optional", False), artifact)
        kind = value["wb_type"]
        if kind == "classesId":
            return cls(kind, value.get("optional", False), class_set=value["class_set"])
        if kind == "ndarray":
            return cls(kind, value.get("optional", False), shape=value.get("shape"))
        if kind == "union":
            return cls(kind, value.get("optional", False), options=tuple(cls.from_json(item, artifact) for item in value["options"]))
        if kind == "const":
            return cls(kind, value.get("optional", False), constant=value["constant"])
        if kind in {"foreignKey", "foreignIndex"}:
            if artifact is None or "table_path" not in value:
                raise ValueError("A stored Table relationship requires its source artifact")
            target = artifact.get(value["table_path"])
            if not isinstance(target, Table):
                raise TypeError("Table relationship target is unavailable")
            return cls(kind, False, table=target, column=value.get("column"))
        if kind not in {"unknown", "any", "none", "number", "boolean", "string", "timestamp", "dictionary", "list", "primaryKey"}:
            # Only our explicit data types can be reconstructed. Never import a
            # module named by an untrusted logged schema.
            from .media import Image, Audio, Video, Html, Plotly, Classes, ImageMask, BoundingBoxes2D
            from .data_types import Histogram
            known = {typ.__name__: typ for typ in [Image, Audio, Video, Html, Plotly, Histogram, Table, Classes, ImageMask, BoundingBoxes2D]}
            if kind not in known:
                raise TypeError(f"Unsupported stored Table type: {kind}")
            kind = known[kind]
        return cls(kind, value.get("optional", True),
                   cls.from_json(value["item_type"], artifact) if "item_type" in value else None,
                   {name: cls.from_json(typ, artifact) for name, typ in value["fields"].items()} if "fields" in value else None)

    def __repr__(self):
        kind = self.kind if isinstance(self.kind, str) else self.kind.__name__
        return f"{kind}{' or None' if self.optional else ''}"


class TableLinkMixin:
    def set_table(self, table):
        self._table = table


class TableIndex(int, TableLinkMixin):
    """An integer index retaining its source table for cross-table references."""

    def __new__(cls, value, table=None):
        result = super().__new__(cls, value)
        result._table = table
        return result

    def get_table(self):
        return self._table

    def get_row(self):
        return dict(zip(self._table.columns, self._table.data[self])) if self._table else {}

    def set_table(self, table):
        self._table = table


class TableKey(str, TableLinkMixin):
    """A string retaining the Table and primary/foreign column it references."""

    def __new__(cls, value, table=None, column=None):
        result = super().__new__(cls, value)
        result._table, result._column = table, column
        return result

    @property
    def _col_name(self):
        return self._column

    @_col_name.setter
    def _col_name(self, value):
        self._column = value

    def set_table(self, table, col_name):
        if col_name not in table.columns:
            raise ValueError("Table key column does not exist")
        self._table, self._column = table, col_name


class Table(Media):
    """Store typed rows; mutations are transactional and logging modes explicit."""

    _MAX_EMBEDDING_DIMENSIONS = 150
    MAX_ROWS = 10_000
    MAX_ARTIFACT_ROWS = 200_000
    _log_type = "table"

    def __init__(self, columns=None, data=None, rows=None, dataframe=None, dtype=None,
                 optional=True, allow_mixed_types=False, log_mode="IMMUTABLE"):
        super().__init__()
        assert log_mode in ["IMMUTABLE", "MUTABLE", "INCREMENTAL"], "Invalid Table log_mode"
        self.log_mode = log_mode
        from .table_construction import select_inputs, declare_types
        self._mixed = bool(allow_mixed_types)
        self.columns, source = select_inputs(columns, data, rows, dataframe)
        self._id = uuid.uuid4().hex
        self._offset, self._generation, self._snapshot_generation = 0, 0, None
        self._base_offset = 0
        self._snapshot = None
        self._pk_col = None
        self._fk_cols = set()
        self.data = []
        declare_types(self, dtype, optional, self._mixed)
        for row in source:
            self.add_data(*row)

    @property
    def _column_types(self):
        from .table_native_schema import legacy_runtime
        from .table_types import TypedDictType
        from .foreign_type_adapter import native_child
        return TypedDictType({name: native_child(legacy_runtime(dtype)) for name, dtype in zip(self.columns, self._types) if name in self._declared_columns})

    def add_data(self, *data):
        """Validate all cells against a candidate schema before appending a row.

        Main callers: training loops, construction, and chart helpers. A rejected
        late cell cannot lock earlier columns to a type from a nonexistent row.
        """
        if len(data) != len(self.columns):
            raise ValueError(f"Expected {len(self.columns)} table cells, received {len(data)}")
        # A reference cell declares its named column before ordinary row
        # validation. This also repairs an omitted constructor dtype. Evaluate
        # every raw cell first, then infer using the last cell for duplicate
        # names, matching W&B's name-keyed schema while keeping failure atomic.
        declared = set(self._declared_columns)
        by_name = dict(zip(self.columns, self._types))
        for name, value in zip(self.columns, data):
            if isinstance(value, (TableKey, TableIndex)):
                from .table_types import TypeRegistry
                typ = ColumnType.from_dtype(TypeRegistry.type_of(value), False)
                index = self.columns.index(name)
                for existing in self.data:
                    typ = typ.assign(existing[index])
                by_name[name] = typ
                declared.add(name)
        missing = [name for name in self.columns if name not in declared]
        if missing:
            raise TypeError(f'Data row contains columns without declared types: {missing}')
        last_indexes = {name: index for index, name in enumerate(self.columns)}
        inferred = {name: by_name[name].assign(data[index]) for name, index in last_indexes.items()}
        candidates = [inferred[name] for name in self.columns]
        self._validate_relationships(candidates)
        if any(new is not old for new, old in zip(candidates, self._types)):
            self.data = [self._wrap_keys(row, candidates) for row in self.data]
        self.data.append(self._wrap_keys(data, candidates))
        self._types = candidates
        self._declared_columns = declared
        self._generation += 1

    def _validate_relationships(self, types, columns=None):
        columns = self.columns if columns is None else columns
        primary = list(dict.fromkeys(name for name, typ in zip(columns, types) if typ.kind == "primaryKey"))
        foreign = {name for name, typ in zip(columns, types) if typ.kind in {"foreignKey", "foreignIndex"}}
        if len(primary) > 1 or self._pk_col is not None and primary != [self._pk_col]:
            raise AssertionError("A Table primary key cannot be removed or changed")
        if self._fk_cols - foreign:
            raise AssertionError("A Table foreign relationship cannot be removed")
        for typ in types:
            if typ.kind in {"primaryKey", "foreignKey", "foreignIndex"} and typ.optional:
                raise AssertionError("Table keys and indexes cannot be optional")
            if typ.kind == "foreignKey" and typ.table is self:
                raise AssertionError("A Table foreign key cannot reference itself")

    def _wrap_keys(self, row, types):
        result = list(row)
        seen = set()
        for index, typ in enumerate(types):
            name = self.columns[index]
            if name in seen:
                continue
            seen.add(name)
            if typ.kind == "primaryKey":
                result[index] = TableKey(result[index], self, self.columns[index])
            elif typ.kind == "foreignKey":
                result[index] = TableKey(result[index], typ.table, typ.column)
            elif typ.kind == "foreignIndex":
                result[index] = TableIndex(result[index], typ.table)
        self._pk_col = next((name for name, typ in zip(self.columns, types) if typ.kind == "primaryKey"), None)
        self._fk_cols = {name for name, typ in zip(self.columns, types) if typ.kind in {"foreignKey", "foreignIndex"}}
        return result

    def add_row(self, *row):
        return self.add_data(*row)

    def _check_structure_change(self):
        if self.log_mode == "INCREMENTAL":
            raise ValueError("INCREMENTAL tables only support appending rows")

    def add_column(self, name, data, optional=False):
        self._check_structure_change()
        import sys
        np = sys.modules.get('numpy')
        assert isinstance(name, str) and name not in self.columns
        assert isinstance(data, list) or np is not None and isinstance(data, np.ndarray)
        assert isinstance(optional, bool)
        values = list(data)
        assert not self.columns or len(values) == len(self.data), "New column must have one value per row"
        from .table_types import UnknownType
        typ = ColumnType.from_dtype(UnknownType(), optional)
        for value in values:
            typ = typ.assign(value)
        rows = [row + [value] for row, value in zip(self.data, values)] if self.columns else [[value] for value in values]
        columns, types = self.columns + [name], self._types + [typ]
        self._validate_relationships(types, columns)
        self.columns.append(name)
        self._declared_columns.add(name)
        self._types = types
        self._optional = self._optional + [optional]
        self.data = [self._wrap_keys(row, self._types) for row in rows]
        self._generation += 1

    def add_computed_columns(self, fn):
        self._check_structure_change()
        additions = []
        for index, row in enumerate(self.data):
            result = fn(TableIndex(index, self), dict(zip(self.columns, row)))
            if not isinstance(result, dict):
                raise TypeError("Computed table rows must be dictionaries")
            if additions and list(result) != list(additions[0]):
                raise ValueError("Computed rows must return the same ordered column names")
            additions.append(result)
        if not additions:
            return
        candidate = Table(columns=list(self.columns), data=self.data, dtype=self._types,
                          optional=self._optional, log_mode=self.log_mode)
        for name in additions[0]:
            candidate.add_column(name, [row[name] for row in additions])
        self.columns[:] = candidate.columns
        self.data = candidate.data
        self._declared_columns = set(candidate._declared_columns)
        self._types, self._optional = candidate._types, candidate._optional
        self.data = [self._wrap_keys(row, self._types) for row in self.data]
        self._generation += 1

    def cast(self, col_name, dtype, optional=False):
        assert col_name in self.columns
        index = self.columns.index(col_name)
        optional = bool(optional)
        candidate = ColumnType.from_dtype(dtype, optional)
        for row in self.data:
            candidate = candidate.assign(row[index])
        if candidate.kind == "primaryKey" and self._pk_col is not None:
            raise AssertionError(f"Cannot have multiple primary keys - {self._pk_col} is already set as the primary key.")
        types = [candidate if name == col_name else typ for name, typ in zip(self.columns, self._types)]
        self._validate_relationships(types)
        rows = [self._wrap_keys(row, types) for row in self.data]
        self._types = types
        self._declared_columns.add(col_name)
        self.data = rows
        if not rows:
            self._pk_col = next((name for name, typ in zip(self.columns, types) if typ.kind == "primaryKey"), None)
            self._fk_cols = {name for name, typ in zip(self.columns, types) if typ.kind in {"foreignKey", "foreignIndex"}}
        self._optional = [optional if name == col_name else flag for name, flag in zip(self.columns, self._optional)]
        self._generation += 1
        # Standard and custom dtype callers receive the same WBType contract as
        # pinned Table.cast. ColumnType remains the native storage/relationship
        # wrapper inside this Table.
        return candidate._runtime_type

    def set_pk(self, col_name):
        assert col_name in self.columns
        from .table_native_types import _PrimaryKeyType
        self.cast(col_name, _PrimaryKeyType())

    def set_fk(self, col_name, table, table_col):
        assert col_name in self.columns
        assert col_name != self._pk_col
        from .table_native_types import _ForeignKeyType
        self.cast(col_name, _ForeignKeyType(table, table_col))

    def get_column(self, name, convert_to=None):
        assert name in self.columns
        assert convert_to is None or convert_to == "numpy"
        index = self.columns.index(name)
        values = [row[index] for row in self.data]
        if convert_to is None:
            return values
        from .values import WBValue
        converted = [value.to_data_array() if isinstance(value, WBValue) else value for value in values]
        return _optional_module("numpy", "Table.get_column(convert_to='numpy')").array(converted)

    def get_index(self):
        return [TableIndex(index, self) for index in range(len(self.data))]

    def index_ref(self, index):
        if not isinstance(index, int) or not -len(self.data) <= index < len(self.data):
            raise IndexError("Table row index is out of range")
        return TableIndex(index, self)

    def has_been_logged(self):
        return self._snapshot is not None

    def get_dataframe(self):
        return _optional_module("pandas", "Table.get_dataframe").DataFrame.from_records(self.data, columns=self.columns)

    def iterrows(self):
        for index, row in enumerate(self.data):
            yield TableIndex(index, self), row

    def _eq_debug(self, other, should_assert=False):
        from .table_type_support import is_numpy_array
        eq = isinstance(other, Table)
        assert not should_assert or eq, (
            f"Found type {other.__class__}, expected {Table}"
        )
        eq = eq and len(self.data) == len(other.data)
        assert not should_assert or eq, (
            f"Found {len(other.data)} rows, expected {len(self.data)}"
        )
        eq = eq and self.columns == other.columns
        assert not should_assert or eq, (
            f"Found columns {other.columns}, expected {self.columns}"
        )
        eq = eq and self._column_types == other._column_types
        assert not should_assert or eq, (
            f"Found column type {other._column_types}, expected column type {self._column_types}"
        )
        if eq:
            for row_ndx in range(len(self.data)):
                for col_ndx in range(len(self.data[row_ndx])):
                    _eq = self.data[row_ndx][col_ndx] == other.data[row_ndx][col_ndx]
                    # equal if all are equal
                    if is_numpy_array(_eq):
                        _eq = ((_eq * -1) + 1).sum() == 0
                    eq = eq and _eq
                    assert not should_assert or eq, (
                        f"Unequal data at row_ndx {row_ndx} col_ndx {col_ndx}: found {other.data[row_ndx][col_ndx]}, expected {self.data[row_ndx][col_ndx]}"
                    )
                    if not eq:
                        return eq
        return eq

    def __eq__(self, other):
        return self._eq_debug(other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def bind_to_run(self, *args, **kwargs):
        """Write a fresh capped view, then apply Media's complete file binding.

        Main callers: explicit public binding and Media-based extensions. Each
        call snapshots current rows, even for IMMUTABLE tables. Incremental
        views begin after the last committed logging cursor; binding itself
        never consumes that cursor. Native run logging keeps its richer artifact
        snapshot independently of this legacy file representation.
        """
        from .media_files import freeze_file
        from .plotly_spec import snapshot_bytes
        offset = self._offset if self.log_mode == "INCREMENTAL" else 0
        payload = {"columns": self.columns, "data": self.data[offset:offset + Table.MAX_ROWS]}
        self._set_file(freeze_file(snapshot_bytes(payload), ".table.json"), is_tmp=True, extension=".table.json")
        return super().bind_to_run(*args, **kwargs)

    def to_json(self, run_or_artifact):
        from .table_serialization import serialize
        return serialize(self, run_or_artifact)

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        from .table_serialization import deserialize
        return deserialize(cls, json_obj, source_artifact)

    @classmethod
    def get_media_subdir(cls):
        return "media/table"
