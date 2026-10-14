"""Preserve Python container identity inside typed Table snapshots.

Architecture: Table/artifact encoding tags tuples and unordered sets; artifact
and Public Api decoding reconstruct only these three known container types.
Plain run dictionaries keep their usual JSON list behavior. Canonical ordering
of encoded set members keeps immutable artifact checksums independent of Python
hash iteration order. No descriptor imports or evaluates Python code.
Main callers: data_types.Encoder, object_codec and Public Api replay.
"""
from __future__ import annotations

import json


def encode_sequence(value, encode):
    """Encode members recursively and retain the concrete container kind.

    Main callers: Encoder.encode at Table/artifact boundaries. A tuple constant
    differs from a list constant, so flattening both to JSON lists loses dtype
    constraints when an immutable Table is read back.
    """
    kinds = {tuple: "tuple", set: "set", frozenset: "frozenset"}
    kind = kinds[type(value)]
    data = [encode(item) for item in value]
    if kind != "tuple":
        data.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return {"_type": "python.sequence", "kind": kind, "data": data}


def decode_sequence(value, decode):
    """Validate a native descriptor, then reconstruct its known container.

    Main callers: artifact object decoding and Public Api hydration. The kind
    selects a local constructor from a fixed map, never a logged module name.
    """
    constructors = {"tuple": tuple, "set": set, "frozenset": frozenset}
    kind, data = value.get("kind"), value.get("data")
    if not isinstance(kind, str) or kind not in constructors or not isinstance(data, list):
        raise ValueError("Stored Python sequence requires tuple/set/frozenset kind and list data")
    return constructors[kind](decode(item) for item in data)
