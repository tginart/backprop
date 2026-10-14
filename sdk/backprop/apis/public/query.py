"""Deterministic filtering and paging for the Foundry event read model.

Architecture: validate query -> resolve nested attributes -> match and order a
bounded snapshot -> expose objects lazily with resumable cursors. Query strings
never become executable Python or SQL. Main callers: Api.runs and artifact lists.
Why this exists: Foundry holds immutable events rather than W&B GraphQL objects.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
import json
from .regex import compile_pattern

MISSING = object()


def lookup(value, path):
    """Resolve a dotted field, preferring a literal key when one exists."""
    if not isinstance(value, Mapping):
        return MISSING
    if path in value:
        return value[path]
    first, separator, rest = path.partition(".")
    if first not in value:
        return MISSING
    return lookup(value[first], rest) if separator else value[first]


def _equal(value, expected):
    if value is MISSING:
        return expected is None
    return value == expected or isinstance(value, list) and not isinstance(expected, list) and expected in value


def _condition(value, condition):
    if not isinstance(condition, Mapping) or not any(str(key).startswith("$") for key in condition):
        return _equal(value, condition)
    answers = []
    for operator, operand in condition.items():
        if operator in {"$in", "$nin"}:
            if not isinstance(operand, list):
                raise ValueError(f"{operator} requires a list")
            match = any(_equal(value, item) for item in operand)
            answers.append(match if operator == "$in" else not match)
        elif operator == "$exists":
            if not isinstance(operand, bool):
                raise ValueError("$exists requires a boolean")
            answers.append((value is not MISSING) == operand)
        elif operator in {"$eq", "$ne"}:
            equal = _equal(value, operand)
            answers.append(equal if operator == "$eq" else not equal)
        elif operator in {"$gt", "$gte", "$lt", "$lte"}:
            values = value if isinstance(value, list) else [value]
            def compare(item):
                if item is MISSING:
                    return False
                try:
                    if operator == "$gt": return item > operand
                    if operator == "$gte": return item >= operand
                    if operator == "$lt": return item < operand
                    return item <= operand
                except TypeError:
                    return False
            answers.append(any(compare(item) for item in values))
        elif operator == "$regex":
            if not isinstance(operand, str):
                raise ValueError("$regex requires a string")
            pattern = compile_pattern(operand)
            values = value if isinstance(value, list) else [value]
            answers.append(any(isinstance(item, str) and pattern.search(item) is not None for item in values))
        else:
            raise ValueError(f"Unsupported filter operator: {operator}")
    return all(answers)


def matches(record, filters):
    """Evaluate documented Mongo-style operators, including nested conjunctions.

    Main callers: Runs and artifact collections. Evaluate every branch so an
    invalid operator is rejected even when an earlier condition did not match.
    """
    if filters is None:
        return True
    if not isinstance(filters, Mapping):
        raise ValueError("filters must be a mapping")
    results = []
    for key, condition in filters.items():
        if not isinstance(key, str):
            raise ValueError("Filter field names must be strings")
        if key in {"$and", "$or", "$nor"}:
            if not isinstance(condition, list) or not condition:
                raise ValueError(f"{key} requires a nonempty list")
            children = [matches(record, child) for child in condition]
            results.append(all(children) if key == "$and" else any(children) if key == "$or" else not any(children))
        elif key.startswith("$"):
            raise ValueError(f"Unsupported filter operator: {key}")
        else:
            results.append(_condition(lookup(record, key), condition))
    return all(results)


def ordered(items, order, record=lambda value: value):
    """Sort mixed values consistently, with a stable input-order tie break."""
    if not isinstance(order, str) or not order or order in {"+", "-"}:
        raise ValueError("order must be a field name, optionally prefixed by + or -")
    field = order.lstrip("+-")
    aliases = {"created_at": "createdAt", "updated_at": "updatedAt", "heartbeat_at": "heartbeatAt"}
    field = aliases.get(field, field)
    if field.startswith("config.") and field.endswith(".value"):
        field = field[:-6]
    def key(item):
        value = lookup(record(item), field)
        if value is MISSING or value is None: return (0, "")
        if isinstance(value, bool): return (1, int(value))
        if isinstance(value, (int, float)): return (2, value)
        if isinstance(value, str): return (3, value)
        return (4, json.dumps(value, sort_keys=True))
    return sorted(items, key=key, reverse=order.startswith("-"))


class Collection:
    """A cached read snapshot with repeatable iteration, indexing and cursors.

    Main callers: public Projects/Runs/Files/Artifacts. A factory is evaluated
    on first access. Like W&B, the cursor identifies the last fetched page's
    final object. Save it at a page boundary to resume without skipping items.
    """

    def __init__(self, factory, per_page=50, *, start=None, scope="", identity=None):
        if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page <= 0:
            raise ValueError("per_page must be a positive integer")
        self.per_page = per_page
        self._factory, self._items = factory, None
        self._scope, self._start, self._position = scope, start, 0
        self._loaded_end = 0
        self._identity = identity or (lambda value: str(value.id))

    def _load(self):
        if self._items is None:
            items = list(self._factory())
            if self._start:
                try:
                    cursor = json.loads(base64.urlsafe_b64decode(self._start.encode()))
                    if cursor["scope"] != self._scope:
                        raise ValueError("Cursor belongs to another query")
                    position = next(i for i, item in enumerate(items) if self._identity(item) == cursor["after"])
                except (ValueError, TypeError, KeyError, StopIteration) as error:
                    raise ValueError("Invalid cursor or referenced object is no longer present") from error
                items = items[position + 1:]
            self._items = items
        return self._items

    def __iter__(self):
        self._position = 0
        return self

    def __next__(self):
        items = self._load()
        if self._position >= len(items):
            raise StopIteration
        if self._position >= self._loaded_end:
            self._loaded_end = min(len(items), self._loaded_end + self.per_page)
        item = items[self._position]
        self._position += 1
        return item

    def __len__(self):
        return len(self._load())

    def __getitem__(self, index):
        items = self._load()
        stop = index.stop if isinstance(index, slice) else index + 1 if index >= 0 else len(items)
        stop = len(items) if stop is None else max(0, stop)
        self._loaded_end = min(len(items), max(self._loaded_end, ((stop + self.per_page - 1) // self.per_page) * self.per_page))
        return items[index]

    @property
    def length(self):
        return len(self)

    @property
    def more(self):
        return self._items is None or self._loaded_end < len(self._items)

    @property
    def cursor(self):
        if not self._loaded_end:
            return self._start
        payload = {"scope": self._scope, "after": self._identity(self._load()[self._loaded_end - 1])}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

    def __repr__(self):
        state = "not loaded" if self._items is None else f"{len(self._items)} items"
        return f"<{type(self).__name__} ({state})>"

    next = __next__
