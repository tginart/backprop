"""Preserve pinned Table constructor input and partial-schema semantics.

Architecture: choose one input source -> validate its concrete container -> keep
raw cells -> declare the zipped dtype/optional prefix -> append typed rows.
Main callers: Table.__init__, also inherited by EvalTable and custom subclasses.
Why this exists: converting a whole NumPy array or DataFrame to lists discards
cell types; eagerly rejecting unequal declaration lengths changes W&B's error
boundary. An undeclared column is distinct from an inferred UnknownType column.
The ordering follows W&B 0.30.0 table.py (THIRD_PARTY_NOTICES.md).
"""
from __future__ import annotations
import sys


def select_inputs(columns, data, rows, dataframe):
    """Choose dataframe > data > rows without converting cells prematurely.

    Main caller: Table.__init__. A list of row iterables remains valid, whereas
    the outer rows container must be a list. Only data accepts NumPy/DataFrames.
    The input columns list retains its observable identity, as in W&B.
    """
    np, pd = sys.modules.get('numpy'), sys.modules.get('pandas')
    if dataframe is None and pd is not None and isinstance(data, pd.DataFrame):
        dataframe = data
    if dataframe is not None:
        assert pd is not None and isinstance(dataframe, pd.DataFrame), 'dataframe argument expects a pandas DataFrame'
        columns = list(dataframe.columns)
        source = (tuple(dataframe[column].values[index] for column in columns) for index in range(len(dataframe)))
    else:
        source = data if data is not None else rows if rows is not None else []
        if not (data is not None and np is not None and isinstance(data, np.ndarray)):
            assert isinstance(source, list), 'data argument expects a `list` object'
        columns = ['Input', 'Output', 'Expected'] if columns is None else columns
    assert isinstance(columns, list), 'columns argument expects a `list` object'
    assert all(type(name) in (str, int) for name in columns), 'columns argument expects list of strings or ints'
    return columns, source


def declare_types(table, dtype, optional, allow_mixed_types):
    """Declare only the zipped prefix, applying casts in their original order.

    Main caller: Table.__init__. Missing declarations can be repaired with cast;
    they cannot accept data or be silently inferred. Repeated names share a type,
    but each declaration still runs validation, including key-role restrictions.
    Placeholder wrappers only keep the native positional layout aligned; the
    public schema explicitly excludes those undeclared columns.
    """
    from .tables import ColumnType
    from .table_types import UnknownType, AnyType
    dtype = AnyType if allow_mixed_types else UnknownType() if dtype is None else dtype
    dtypes = dtype if isinstance(dtype, list) else [dtype] * len(table.columns)
    flags = optional if isinstance(optional, list) else [optional] * len(table.columns)
    table._types = [ColumnType.from_dtype(UnknownType(), False) for _ in table.columns]
    table._optional = [False] * len(table.columns)
    table._declared_columns = set()
    for name, typ, flag in zip(table.columns, dtypes, flags):
        table.cast(name, typ, flag)
    table._generation = 0


def require_declared(table):
    """Reject undeclared columns at their actual data/publication boundary.

    Main caller: Encoder's ordinary Table publication.
    Public artifact serialization already looks up names in the sparse schema.
    Native serialization must enforce the same boundary before journaling rather
    than persisting placeholder types as if they were caller declarations.
    """
    missing = [name for name in table.columns if name not in table._declared_columns]
    if missing:
        raise KeyError(missing[0])
