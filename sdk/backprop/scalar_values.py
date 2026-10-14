"""Translate JSON-safe non-finite scalars back to Python numeric values.

Architecture: Foundry records use explicit typed descriptors -> in-process
summary/config reads and reducers recover numeric semantics. Other rich values
remain descriptors so decoding does not accidentally download media.
Main callers: metric reduction, Summary reads and resumed Config hydration.
"""

_NUMBERS = {"NaN": float("nan"), "Infinity": float("inf"), "-Infinity": -float("inf")}


def decode_numbers(value):
    if isinstance(value, dict):
        if value.get("_type") == "number" and value.get("value") in _NUMBERS:
            return _NUMBERS[value["value"]]
        return {key: decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_numbers(item) for item in value]
    return value


def numeric_value(value):
    if isinstance(value, dict) and value.get("_type") == "number":
        return _NUMBERS.get(value.get("value"))
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
