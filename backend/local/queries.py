"""
Purpose: Translate only the history queries that Backprop produces.
Input: A Backprop SELECT statement with encoded literals.
Output: A parameterized SQLite query over the canonical event records.
Note: The server never executes SQL supplied by an HTTP caller.
"""
from __future__ import annotations

import re

from .config import STREAM_RID


LITERAL = r"decode\(unhex\('([0-9a-f]*)'\), 'UTF-8'\)"
FIELDS = "schema_version,event_id,run_id,project,entity,event_type,timestamp,payload"
MAX_QUERY_ROWS = 1_000_000


def translate_query(query: str) -> tuple[str, list]:
    """
    Purpose: Accept the SDK pagination and browser archive query shapes.
    Input: One SELECT statement for the local event dataset.
    Output: A SQLite SELECT statement and its bound parameters.
    Method:
        1. Validate the table, order, and row limit.
        2. Consume each supported predicate without residual text.
        3. Bind all values separately from the SQLite statement.
    """
    pattern = (
        rf"SELECT \* FROM `{re.escape(STREAM_RID)}`"
        r"(?: WHERE (.+?))? ORDER BY (timestamp(?: DESC, event_id DESC)?)"
        r" LIMIT ([0-9]+)"
    )
    match = re.fullmatch(pattern, query)
    if not match:
        raise ValueError("Only Backprop history queries are supported locally")
    where, order, limit = match.groups()
    limit = int(limit)
    if not 1 <= limit <= MAX_QUERY_ROWS:
        raise ValueError("History query row limit is out of range")
    predicates, parameters = _translate_predicates(where or "")
    sql = f"SELECT {FIELDS} FROM events"
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    sql += " ORDER BY " + order + " LIMIT ?"
    return sql, [*parameters, limit]


def _translate_predicates(where: str) -> tuple[list[str], list[str]]:
    """
    Purpose: Parse equality, control recovery, and descending page predicates.
    Input: The WHERE clause from one validated query shape.
    Output: Safe predicates and decoded UTF-8 values.
    """
    predicates, parameters = [], []
    equality = re.compile(rf"(project|entity|run_id|event_type) = {LITERAL}")
    like = re.compile(rf"payload LIKE {LITERAL}")
    cursor = re.compile(
        rf"\(timestamp < {LITERAL} OR \(timestamp = {LITERAL}"
        rf" AND event_id < {LITERAL}\)\)"
    )
    while where:
        match = equality.match(where)
        if match:
            field, value = match.groups()
            predicates.append(field + " = ?")
            parameters.append(_decode(value))
        else:
            match = like.match(where)
            if match:
                predicates.append("payload LIKE ?")
                parameters.append(_decode(match[1]))
            else:
                match = cursor.match(where)
                if not match:
                    raise ValueError("Unsupported Backprop history predicate")
                first, second, identifier = map(_decode, match.groups())
                if first != second:
                    raise ValueError("History cursor timestamps must match")
                predicates.append("(timestamp < ? OR (timestamp = ? AND event_id < ?))")
                parameters.extend([first, second, identifier])
        where = where[match.end():]
        if where:
            if not where.startswith(" AND "):
                raise ValueError("Unsupported Backprop history predicate")
            where = where[5:]
    return predicates, parameters


def _decode(value: str) -> str:
    return bytes.fromhex(value).decode("utf-8")
