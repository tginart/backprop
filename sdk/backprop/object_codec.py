"""Typed object snapshots inside immutable artifact manifests.

Architecture: an object becomes one typed JSON entry; embedded media becomes
digest-named sibling entries; decoding reads and verifies those same entries.
Artifact encoding always captures full Tables and never advances run cursors.
Main callers: Artifact.add/get and public artifact retrieval.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import mimetypes
import warnings
from pathlib import Path

OBJECT_SUFFIXES = (".table.json", ".image-file.json", ".audio-file.json", ".video-file.json", ".html-file.json", ".classes.json", ".mask.json", ".bounding-boxes.json", ".joined-table.json", ".graph-file.json", ".partitioned-table.json")


def object_suffixes():
    """Resolve local extension names without importing classes from stored data.

    Main callers: Artifact.get. Built-in historical suffixes remain readable;
    additional suffixes come only from the same explicit WBValue registry.
    """
    from .values import WBValue
    return tuple(dict.fromkeys((*OBJECT_SUFFIXES, *("." + tag + ".json" for tag in WBValue.type_mapping()))))


def object_suffix(obj):
    from .tables import Table
    from .media import Image, Audio, Video, Html, Classes, ImageMask, BoundingBoxes2D
    from .partitioned_table import PartitionedTable
    from .extended_tables import JoinedTable
    from .graph import Graph
    # Follow the pinned artifact value-family whitelist, including user-defined
    # subclasses. Graph is the documented native addition for saved captures.
    supported = (Table, Image, Audio, Video, Html, Classes, ImageMask, BoundingBoxes2D,
                 PartitionedTable, JoinedTable, Graph)
    if isinstance(obj, supported) and obj._log_type:
        return "." + obj._log_type + ".json"
    raise TypeError(f"Artifact.add does not support {type(obj).__name__}")


def encode_object(obj, artifact):
    """Store media dependencies, then return a deterministic full object value."""
    from .partitioned_table import PartitionedTable
    if isinstance(obj, PartitionedTable): return obj.to_json(artifact)
    from .extended_tables import JoinedTable
    if isinstance(obj, JoinedTable): return obj.to_json(artifact)
    from .data_types import Encoder

    def stage(path, data=None, name=None):
        data = Path(path).read_bytes() if path is not None else data
        if not isinstance(data, bytes):
            raise TypeError("Artifact media encoding requires bytes")
        sha = hashlib.sha256(data).hexdigest()
        suffix = Path(name or "").suffix
        logical = "media/" + sha + suffix
        if logical not in artifact._entries:
            scratch = artifact._temporary_root() / ("media-" + sha + suffix)
            scratch.write_bytes(data)
            artifact.add_file(scratch, logical)
        entry = artifact.get_entry(logical)
        return {"artifact_path": logical, "path": logical, "sha256": entry.sha256,
                "size": entry.size, "mime_type": mimetypes.guess_type(name or "")[0]}

    class ObjectEncoder(Encoder):
        def _table(self, table):
            from .json_keys import table_schema_fields
            if len(table.data) > table.MAX_ARTIFACT_ROWS:
                import backprop
                settings = getattr(getattr(backprop, "run", None), "settings", None)
                if getattr(settings, "table_raise_on_max_row_limit_exceeded", False) or getattr(settings, "strict", False):
                    raise ValueError(f"Artifact table exceeds {table.MAX_ARTIFACT_ROWS} rows; split the table explicitly")
                warnings.warn(f"Truncating artifact Table to {table.MAX_ARTIFACT_ROWS} rows", RuntimeWarning, stacklevel=3)
            result = {"_type": "table", "columns": list(table.columns), "data": self.encode(table.data[:table.MAX_ARTIFACT_ROWS]),
                      **table_schema_fields([typ.to_json(artifact) for typ in table._types]), "log_mode": table.log_mode}
            if table.log_mode == "INCREMENTAL":
                result.update(increment_num=getattr(table, "_increment_num", 0),
                              previous_increments_paths=list(getattr(table, "_previous_increments_paths", [])))
            return result

    encoder = ObjectEncoder(stage, context="artifact")
    encoder._schema_artifact = artifact
    result = encoder.encode(obj)
    if getattr(obj, "_log_type", None) == "mask":
        result["_type"] = "mask"
    return result


def decode_object(value, artifact):
    """Reconstruct values from verified local or remote manifest bytes.

    Main callers: Artifact.get. No untrusted Python is imported or executed;
    the descriptor must name one of the supported native data types.
    """
    from .data_types import Histogram
    from .tables import Table, ColumnType
    from .media import Image, Audio, Video, Html, Plotly, Classes, ImageMask, BoundingBoxes2D, EncodedMedia
    if isinstance(value, list):
        return [decode_object(item, artifact) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("_type")
    if kind == "python.sequence":
        from .sequence_codec import decode_sequence
        return decode_sequence(value, lambda item: decode_object(item, artifact))
    if kind == "number":
        return {"NaN": float("nan"), "Infinity": float("inf"), "-Infinity": -float("inf")}[value["value"]]
    if kind == "timestamp":
        return date.fromisoformat(value["value"]) if len(value["value"]) == 10 else datetime.fromisoformat(value["value"])
    if kind == "ndarray":
        return decode_ndarray(value, decode_object(value["data"], artifact))
    if kind == "partitioned-table":
        from .partitioned_table import PartitionedTable
        return PartitionedTable.from_json(value, artifact)
    if kind == "joined-table":
        from .extended_tables import JoinedTable
        return JoinedTable.from_json(value, artifact)
    if kind == "table" and isinstance(value.get("column_types"), dict):
        return Table.from_json(value, artifact)
    if kind == "table":
        types = [ColumnType.from_json(item, artifact) for item in value.get("column_types", [])]
        if value.get("log_mode") == "INCREMENTAL":
            from .incremental_artifacts import decode_increment_rows
            data = decode_increment_rows(value, artifact)
        else:
            data = decode_object(value["data"], artifact)
        from .extended_tables import EvalTable
        roles = value.get('evaluation_roles')
        cls = EvalTable if roles is not None else Table
        arguments = {key: roles[key] for key in ('input_columns', 'output_columns', 'score_columns')} if roles is not None else {}
        table = cls(columns=value["columns"], data=data,
                      dtype=types or None, optional=[typ.optional for typ in types] if types else True,
                      log_mode=value.get("log_mode", "IMMUTABLE"), **arguments)
        if table.log_mode == "INCREMENTAL":
            table._increment_num = value.get("increment_num", 0)
            table._previous_increments_paths = list(value.get("previous_increments_paths", []))
        return table
    if kind in {"graph", "graph-file"}:
        from .graph import Graph
        return Graph.from_json(value, artifact)
    if kind == "classes":
        return Classes.from_json(value, artifact)
    if kind == "bounding-boxes":
        from .table_image_type import _numeric_labels
        return BoundingBoxes2D({"box_data": value["box_data"],
            "class_labels": _numeric_labels(value.get("class_labels", {}))}, "")
    if kind == "histogram":
        return Histogram(np_histogram=(decode_object(value["values"], artifact), decode_object(value["bins"], artifact)))
    if kind in {"image", "audio", "video", "html", "mask"}:
        path = value.get("artifact_path") or value["path"]
        data = artifact.get_entry(path)._read_verified_bytes()
        if kind == "mask":
            result = ImageMask.__new__(ImageMask)
            result._key = ""
            from .annotation_values import temporary_path
            from .media_base import Media
            path_copy = temporary_path(".mask.png"); path_copy.write_bytes(data)
            Media.__init__(result)
            result._set_file(str(path_copy), is_tmp=True, extension=".mask.png")
            result._val = {"class_labels": value.get("class_labels", {})}
        elif kind == "image":
            result = Image(data, caption=value.get("caption"))
        elif kind == "audio":
            result = Audio(data, caption=value.get("caption"))
        elif kind == "video":
            result = Video(data, caption=value.get("caption"), format=value.get("format") or value.get("mime_type", "").split("/")[-1])
        else:
            result = Html(data, inject=False)
        transport_keys = {"_type", "artifact_path", "path", "sha256", "size", "caption"}
        result.metadata = {key: decode_object(item, artifact) for key, item in value.items() if key not in transport_keys}
        result._artifact_source = artifact
        result._artifact_content_path = path
        return result
    if kind == "plotly":
        from .plotly_resources import restore_resources
        result = Plotly(decode_object(value["spec"], artifact))
        return restore_resources(result, value.get("resources", []), artifact)
    if kind == "plot":
        from .plot import CustomChart, CustomChartSpec
        fields = value.get("spec_fields", value["fields"])
        return CustomChart(decode_object(value["table"], artifact),
            CustomChartSpec(value["spec_name"], fields, value["string_fields"], split_table=value.get("split_table", False)))
    from .values import WBValue
    if kind in WBValue.type_mapping():
        return WBValue.init_from_json(value, artifact)
    return {key: decode_object(item, artifact) for key, item in value.items()}


def decode_ndarray(value, decoded_data):
    """Rebuild a recorded array without inventing dimensions from nested cells.

    Main callers: artifact object decoding and public history replay. JSON lists
    lose dimensions after an empty axis, and object arrays may store lists as
    individual cells. The declared shape defines the array boundary; we validate
    every visible dimension before constructing either representation.
    """
    from .tables import _optional_module
    np = _optional_module("numpy", "Stored ndarray reconstruction")
    shape = value.get("shape")
    if not isinstance(shape, list) or any(type(size) is not int or size < 0 for size in shape):
        raise ValueError("Stored ndarray shape must contain nonnegative integers")
    dtype = np.dtype(value["dtype"])
    if dtype.hasobject:
        if dtype.fields:
            raise ValueError("Structured object ndarray dtypes are unsupported")
        # Assignment at the declared depth preserves list/dict/array objects as
        # cells instead of letting np.asarray grow additional array dimensions.
        cells = []
        def collect(data, depth, index):
            if depth == len(shape):
                cells.append((index, data))
                return
            if not isinstance(data, list) or len(data) != shape[depth]:
                raise ValueError("Stored ndarray shape does not match its content")
            for offset, item in enumerate(data):
                collect(item, depth + 1, index + (offset,))
        collect(decoded_data, 0, ())
        array = np.empty(tuple(shape), dtype=dtype)
        for index, data in cells:
            array[index] = data
        return array
    array = np.asarray(decoded_data, dtype=dtype)
    if list(array.shape) == shape:
        return array
    # Empty arrays encode only the shape through their first zero axis. Accept
    # missing trailing dimensions, never a conflicting visible prefix.
    if array.size == 0 and 0 in shape and list(array.shape) == shape[:len(array.shape)]:
        return array.reshape(tuple(shape))
    raise ValueError("Stored ndarray shape does not match its content")
