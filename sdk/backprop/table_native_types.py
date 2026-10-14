"""Native media and Table adapters for the pinned W&B type engine.

Architecture: imported native value classes -> source WBType adapters -> shared
TypeRegistry. Assignment and public params use the exact pinned algorithms;
table_native_schema handles the separate native history/artifact representation.
Main callers: ColumnType and the W&B-compatible _dtypes import surface.
Why this exists: a media example, a media class and an inferred value are
different type inputs, and relationships must compare actual Table identities.

Adapted from W&B 0.30.0, commit e5ec9dc9236d1a4fbb9fe7c0795bbccdff1e0e77.
MIT License

Copyright (c) 2021 Weights and Biases, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

from . import table_types as _dtypes
from .tables import Table, TableKey as _TableKey, TableIndex as _TableIndex
from .media import Classes, Image, Audio, Video, Html

runid = SimpleNamespace(generate_id=lambda: uuid.uuid4().hex)

class _PrimaryKeyType(_dtypes.Type):
    name = "primaryKey"
    legacy_names = ["wandb.TablePrimaryKey"]

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, (_dtypes.StringType, _PrimaryKeyType)):
            return self
        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, _TableKey):
            raise TypeError("py_obj must be a wandb.Table")
        else:
            return cls()

class _ForeignKeyType(_dtypes.Type):
    name = "foreignKey"
    legacy_names = ["wandb.TableForeignKey"]
    types = [_TableKey]

    def __init__(self, table, col_name):
        assert isinstance(table, Table)
        assert isinstance(col_name, str)
        assert col_name in table.columns
        self.params.update({"table": table, "col_name": col_name})

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, _dtypes.StringType):
            return self
        elif (
            isinstance(wb_type, _ForeignKeyType)
            and id(self.params["table"]) == id(wb_type.params["table"])
            and self.params["col_name"] == wb_type.params["col_name"]
        ):
            return self

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, _TableKey):
            raise TypeError("py_obj must be a _TableKey")
        else:
            return cls(py_obj._table, py_obj._col_name)

    def to_json(self, artifact=None):
        res = super().to_json(artifact)
        if artifact is not None:
            table_name = f"media/tables/t_{runid.generate_id()}"
            entry = artifact.add(self.params["table"], table_name)
            res["params"]["table"] = entry.path
        else:
            raise AssertionError(
                "_ForeignKeyType does not support serialization without an artifact"
            )
        return res

    @classmethod
    def from_json(
        cls,
        json_dict,
        artifact,
    ):
        table = None
        col_name = None
        if artifact is None:
            raise AssertionError(
                "_ForeignKeyType does not support deserialization without an artifact"
            )
        else:
            table = artifact.get(json_dict["params"]["table"])
            col_name = json_dict["params"]["col_name"]

        if table is None:
            raise AssertionError("Unable to deserialize referenced table")

        return cls(table, col_name)

class _ForeignIndexType(_dtypes.Type):
    name = "foreignIndex"
    legacy_names = ["wandb.TableForeignIndex"]
    types = [_TableIndex]

    def __init__(self, table):
        assert isinstance(table, Table)
        self.params.update({"table": table})

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, _dtypes.NumberType):
            return self
        elif isinstance(wb_type, _ForeignIndexType) and id(self.params["table"]) == id(
            wb_type.params["table"]
        ):
            return self

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, _TableIndex):
            raise TypeError("py_obj must be a _TableIndex")
        else:
            return cls(py_obj._table)

    def to_json(self, artifact=None):
        res = super().to_json(artifact)
        if artifact is not None:
            table_name = f"media/tables/t_{runid.generate_id()}"
            entry = artifact.add(self.params["table"], table_name)
            res["params"]["table"] = entry.path
        else:
            raise AssertionError(
                "_ForeignIndexType does not support serialization without an artifact"
            )
        return res

    @classmethod
    def from_json(
        cls,
        json_dict,
        artifact,
    ):
        table = None
        if artifact is None:
            raise AssertionError(
                "_ForeignIndexType does not support deserialization without an artifact"
            )
        else:
            table = artifact.get(json_dict["params"]["table"])

        if table is None:
            raise AssertionError("Unable to deserialize referenced table")

        return cls(table)

class _TableType(_dtypes.Type):
    name = "table"
    legacy_names = ["wandb.Table"]
    types = [Table]

    def __init__(self, column_types=None):
        if column_types is None:
            column_types = _dtypes.UnknownType()
        if isinstance(column_types, dict):
            column_types = _dtypes.TypedDictType(column_types)
        elif not (
            isinstance(column_types, (_dtypes.TypedDictType, _dtypes.UnknownType))
        ):
            raise TypeError("column_types must be a dict or TypedDictType")

        self.params.update({"column_types": column_types})

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, _TableType):
            column_types = self.params["column_types"].assign_type(
                wb_type.params["column_types"]
            )
            if not isinstance(column_types, _dtypes.InvalidType):
                return _TableType(column_types)

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, Table):
            raise TypeError("py_obj must be a wandb.Table")
        else:
            return cls(py_obj._column_types)

class _ClassesIdType(_dtypes.Type):
    name = "classesId"
    legacy_names = ["wandb.Classes_id"]
    types = [Classes]

    def __init__(
        self,
        classes_obj: Classes | None = None,
        valid_ids: _dtypes.UnionType | None = None,
    ):
        if valid_ids is None:
            valid_ids = _dtypes.UnionType()
        elif isinstance(valid_ids, list):
            valid_ids = _dtypes.UnionType(
                [_dtypes.ConstType(item) for item in valid_ids]
            )
        elif isinstance(valid_ids, _dtypes.UnionType):
            valid_ids = valid_ids
        else:
            raise TypeError("valid_ids must be None, list, or UnionType")

        if classes_obj is None:
            classes_obj = Classes(
                [
                    {"id": _id.params["val"], "name": str(_id.params["val"])}
                    for _id in valid_ids.params["allowed_types"]
                ]
            )
        elif not isinstance(classes_obj, Classes):
            raise TypeError("valid_ids must be None, or instance of Classes")
        else:
            valid_ids = _dtypes.UnionType(
                [
                    _dtypes.ConstType(class_obj["id"])
                    for class_obj in classes_obj._class_set
                ]
            )

        self.wb_classes_obj_ref = classes_obj
        self.params.update({"valid_ids": valid_ids})

    def assign(self, py_obj: Any | None = None) -> _dtypes.Type:
        return self.assign_type(_dtypes.ConstType(py_obj))

    def assign_type(self, wb_type: _dtypes.Type) -> _dtypes.Type:
        valid_ids = self.params["valid_ids"].assign_type(wb_type)
        if not isinstance(valid_ids, _dtypes.InvalidType):
            return self

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj: Any | None = None) -> _dtypes.Type:
        return cls(py_obj)

    def to_json(self, artifact: Artifact | None = None) -> dict[str, Any]:
        cl_dict = super().to_json(artifact)
        # TODO (tss): Refactor this block with the similar one in wandb.Image.
        # This is a bit of a smell that the classes object does not follow
        # the same file-pattern as other media types.
        if artifact is not None:
            class_name = os.path.join("media", "cls")
            classes_entry = artifact.add(self.wb_classes_obj_ref, class_name)
            cl_dict["params"]["classes_obj"] = {
                "type": "classes-file",
                "path": classes_entry.path,
                "digest": classes_entry.digest,  # is this needed really?
            }
        else:
            cl_dict["params"]["classes_obj"] = self.wb_classes_obj_ref.to_json(artifact)
        return cl_dict

    @classmethod
    def from_json(
        cls,
        json_dict: dict[str, Any],
        artifact: Artifact | None = None,
    ) -> _dtypes.Type:
        classes_obj = None
        if (
            json_dict.get("params", {}).get("classes_obj", {}).get("type")
            == "classes-file"
        ):
            if artifact is not None:
                classes_obj = artifact.get(
                    json_dict.get("params", {}).get("classes_obj", {}).get("path")
                )
                assert classes_obj is None or isinstance(classes_obj, Classes)
            else:
                raise RuntimeError("Expected artifact to be non-null.")
        else:
            classes_obj = Classes.from_json(
                json_dict["params"]["classes_obj"], artifact
            )

        return cls(classes_obj)

class _AudioFileType(_dtypes.Type):
    name = "audio-file"
    types = [Audio]


class _VideoFileType(_dtypes.Type):
    name = "video-file"
    types = [Video]


class _HtmlFileType(_dtypes.Type):
    name = "html-file"
    types = [Html]


def register_native_types():
    """Register the complete supported media family after imports are ready.

    Main callers: the end of data_types.py. Delaying this one call avoids an
    import cycle while preserving exact TypeRegistry lookup before first use.
    """
    from .table_image_type import _ImageFileType
    for handler in (*NATIVE_TYPES, _ImageFileType):
        _dtypes.TypeRegistry.add(handler)


NATIVE_TYPES = (_PrimaryKeyType, _ForeignKeyType, _ForeignIndexType, _TableType,
                _ClassesIdType, _AudioFileType, _VideoFileType, _HtmlFileType)
