"""Decode actual pinned W&B history paths without changing metric tag names.

Architecture: checksum-verified transaction Record -> HistoryItem path labels
-> nested rich values. Main callers: the offline runtime comparison and its
portable regression test. Why this exists: slash characters inside a metric
tag are literal; only protobuf nested_key labels create object nesting.
"""
from __future__ import annotations

import json


def history_row(items):
    """Rebuild one history row using the exact protobuf path boundaries.

    Main callers: tensorboard_gap_runtime_probe after raw output is saved.
    Core flattens histogram and media objects into several HistoryItems. A
    tag like train/loss stays one dictionary key, while [weights, bins] is a
    two-level value. Conflicting scalar/object paths raise instead of dropping
    evidence from the upstream observation.
    """
    row = {}
    for item in items:
        labels = list(item.nested_key) or [item.key]
        if not all(labels):
            raise ValueError("Empty W&B history path label")
        target = row
        for label in labels[:-1]:
            target = target.setdefault(label, {})
            if not isinstance(target, dict):
                raise ValueError("Conflicting W&B scalar and object history paths")
        if labels[-1] in target:
            raise ValueError("Duplicate W&B history path")
        target[labels[-1]] = json.loads(item.value_json)
    return row
