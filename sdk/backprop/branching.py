"""Run moments and append-only history branch reconstruction.

Architecture: validate a run/step URI -> reconstruct visible source events ->
retain history through an inclusive cutoff. Rewinds append a truncation record;
forks copy the retained records with new envelope identities. Neither operation
edits existing Foundry records or depends on a mutable source after creation.
Main callers: Settings validation and Run initialization/replay.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from urllib.parse import urlparse, parse_qs


@dataclass
class RunMoment:
    run: str
    value: int | float
    metric: str = "_step"

    def __post_init__(self):
        if self.metric != "_step":
            raise ValueError("Only the metric '_step' is supported")
        if not isinstance(self.value, (int, float)):
            raise TypeError("Run moment value must be numeric")
        if not math.isfinite(self.value):
            raise ValueError("Run moment value must be finite")
        if not isinstance(self.run, str):
            raise TypeError("Run moment run must be a string")

    @classmethod
    def from_uri(cls, uri):
        message = "Expected run moment '<run>?_step=<numeric_value>'"
        if not isinstance(uri, str):
            raise TypeError(message)
        parsed = urlparse("runmoment://" + uri)
        query = parse_qs(parsed.query)
        if not parsed.netloc or parsed.path or parsed.params or parsed.fragment or set(query) != {"_step"}:
            raise ValueError(message)
        try:
            value = query["_step"][0]
            return cls(parsed.netloc, int(value) if value.isdigit() else float(value))
        except (ValueError, IndexError) as error:
            raise ValueError(message) from error


def visible_events(events):
    """Remove superseded history before resuming an append-only run.

    Main callers: Run resume and source snapshots for fork/rewind. Explicit
    summaries before a rewind are superseded by its authoritative snapshot.
    """
    result = []
    for event in events:
        if event["type"] == "history_truncate":
            step = event["payload"]["step"]
            result = [item for item in result if item["type"] not in {"summary", "history_truncate"}
                      and (item["type"] != "history" or item["payload"]["step"] <= step)]
        result.append(event)
    return result


def through_step(events, cutoff):
    """Select history and definitions needed to reconstruct an inclusive cut."""
    return [event for event in visible_events(events)
            if event["type"] in {"run_start", "config", "metric_definition", "run_metadata"}
            or (event["type"] == "history" and event["payload"]["step"] <= cutoff)]
