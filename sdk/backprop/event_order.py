"""Canonical replay order for SDK readers and the browser.

Architecture: validate repeated identities -> deduplicate -> order by UTC time
and event ID. Writers retain IDs on retries. Equal timestamps never depend on
the order in which the archive and live stream happened to return rows.
Main callers: experiment replay and mutable artifact views.
"""
from datetime import datetime


def ordered_events(events):
    """Return a deterministic snapshot, rejecting conflicting retry records."""
    unique = {}
    for event in events:
        identity = event["event_id"]
        if identity in unique and unique[identity] != event:
            raise ValueError(f"Conflicting records for event ID: {identity}")
        unique[identity] = event
    return sorted(unique.values(), key=lambda event: (
        datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")), event["event_id"]))
