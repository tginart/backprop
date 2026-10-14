"""Lossless, non-executable constant values inside native Table dtype schemas.

Architecture: Python scalar/container -> tagged JSON value -> original scalar or
container. Tags apply only at this schema boundary and never request imports.
Main callers: table_dtype_bridge capture/restore. Sets are ordered canonically so
equivalent immutable artifact schemas have the same content identity.
"""
import json
import math


def encode_value(value):
    if value is None or type(value) in {str, bool, int}:
        return value
    # Pinned artifact JSON accepts subclasses of its scalar primitives, notably
    # numpy.float64 and numpy.str_. Normalize only those JSON-compatible classes;
    # numpy.int64/date/object constants still fail explicitly like plain JSON.
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        value = float(value)
        return value if math.isfinite(value) else {"kind": "float", "value": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
    if type(value) in {list, tuple, set, frozenset}:
        items = [encode_value(item) for item in value]
        if type(value) in {set, frozenset}:
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return {"kind": type(value).__name__, "items": items}
    if type(value) is dict:
        return {"kind": "dict", "items": [[encode_value(key), encode_value(item)] for key, item in value.items()]}
    raise TypeError(f"Table dtype constant {type(value).__name__} cannot be serialized without executable Python")


def decode_value(value):
    if not isinstance(value, dict):
        return value
    kind = value.get("kind")
    if kind == "float" and set(value) == {"kind", "value"}:
        return {"NaN": float("nan"), "Infinity": float("inf"), "-Infinity": -float("inf")}[value["value"]]
    if set(value) != {"kind", "items"} or not isinstance(value["items"], list):
        raise TypeError("Invalid native Table dtype constant")
    if kind == "dict":
        return {decode_value(key): decode_value(item) for key, item in value["items"]}
    if kind in {"list", "tuple", "set", "frozenset"}:
        return {"list": list, "tuple": tuple, "set": set, "frozenset": frozenset}[kind](decode_value(item) for item in value["items"])
    raise TypeError("Unknown native Table dtype constant kind")
