"""
Purpose: Parse shared history queries into native Firestore constraints.
Input: SDK predicates with encoded literals or the dashboard archive query.
Output: Exact scope filters, page cursors, payload patterns, and a bounded row limit.
Note: This module does not execute SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
import json
import re

from .config import STREAM_RID
from backprop_backend.records import FIELDS


SCOPE_FIELDS = ("project", "entity", "run_id", "event_type")
LITERAL = r"decode\(unhex\('([0-9a-f]*)'\), 'UTF-8'\)"
SORT_PREFIX_BYTES = 256


@dataclass(frozen=True)
class HistoryQuery:
    """
    Purpose: Describe one validated history request without database syntax.
    Input: Exact scope predicates, cursors, and the requested order.
    Output: An immutable plan for the native Firestore adapter.
    """

    filters: tuple[tuple[str, str], ...]
    cursors: tuple[tuple[str, str], ...]
    patterns: tuple[str, ...]
    descending: bool
    limit: int


def parse_query(query: str) -> HistoryQuery:
    """
    Purpose: Accept the same bounded query shapes as the existing backends.
    Input: A SELECT statement with UTF-8 hex literals.
    Output: A detached plan or an error before any database operation.
    """
    match = re.fullmatch(
        rf"SELECT \* FROM `{re.escape(STREAM_RID)}`"
        r"(?: WHERE (.+?))? ORDER BY (timestamp(?: DESC, event_id DESC)?)"
        r" LIMIT ([0-9]+)", query,
    )
    if not match or not 1 <= int(match[3]) <= 1_000_000:
        raise ValueError("Only bounded Backprop history queries are supported")
    where, order, limit = match.groups()
    filters, cursors, patterns = [], [], []
    while where:
        equality = re.match(rf"(project|entity|run_id|event_type) = {LITERAL}", where)
        pattern = re.match(rf"payload LIKE {LITERAL}", where)
        cursor = re.match(
            rf"\(timestamp < {LITERAL} OR \(timestamp = {LITERAL}"
            rf" AND event_id < {LITERAL}\)\)", where,
        )
        if equality:
            filters.append((equality[1], _decode(equality[2])))
            consumed = equality.end()
        elif pattern:
            patterns.append(_decode(pattern[1]))
            consumed = pattern.end()
        elif cursor:
            first, second, identifier = map(_decode, cursor.groups())
            if first != second:
                raise ValueError("History cursor timestamps must match")
            cursors.append((first, identifier))
            consumed = cursor.end()
        else:
            raise ValueError("Unsupported Backprop history predicate")
        where = where[consumed:]
        if where:
            if not where.startswith(" AND "):
                raise ValueError("Unsupported Backprop history predicate")
            where = where[5:]
    return HistoryQuery(tuple(filters), tuple(cursors), tuple(patterns),
                        order.endswith("DESC"), int(limit))


def scope_key(filters: tuple[tuple[str, str], ...]) -> str:
    encoded = json.dumps(sorted(filters), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def scope_keys(record: dict) -> list[str]:
    return [scope_key(tuple((field, record[field]) for field in fields))
            for size in range(len(SCOPE_FIELDS) + 1)
            for fields in itertools.combinations(SCOPE_FIELDS, size)]


def sort_key(timestamp: str, event_id: str) -> str:
    stamp = timestamp.encode().replace(b"\x00", b"\x00\xff")
    return (stamp + b"\x00\x00" + event_id.encode())[:SORT_PREFIX_BYTES].hex()


def matches(record: dict, query: HistoryQuery) -> bool:
    """
    Purpose: Preserve exact predicates after the bounded native index lookup.
    Input: A canonical record and its query plan.
    Output: Whether every scope, cursor, and SQL LIKE predicate matches.
    """
    if any(record[field] != value for field, value in query.filters):
        return False
    if any((record["timestamp"], record["event_id"]) >= cursor for cursor in query.cursors):
        return False
    for pattern in query.patterns:
        expression = "".join(".*" if value == "%" else "." if value == "_"
                             else re.escape(value) for value in pattern)
        if re.fullmatch(expression, record["payload"], re.DOTALL | re.I | re.ASCII) is None:
            return False
    return True


def _decode(value: str) -> str:
    return bytes.fromhex(value).decode("utf-8")
