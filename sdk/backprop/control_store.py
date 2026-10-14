"""Bounded readers for process-targeted control events in the existing stream.

Architecture: capture offsets before announcing a process -> read small live
pages -> periodically query only that process's stop requests for archive recovery.
A UUID identifies a process lifetime, never a reused run ID. Readers retain their
cursor after errors and never load experiment history to look for a stop request.
Main callers: RunStopChecker through each store's control_reader factory.
"""
from __future__ import annotations

import time


REQUEST_TYPE = "run_stop_request"
POLL_INTERVAL = 15
ARCHIVE_INTERVAL = 300


def active_stop_controls(events):
    """Find connected process lifetimes in one scoped, ordered run snapshot.

    Main caller: public Run.stop. A shared experiment has independent control
    readers, so its primary's metadata alone cannot address every writer. Start
    and finish records identify each lifetime; an ordinary resume replaces its
    preceding process. No timestamp cutoff is used for requests from a different
    machine. Processes first observed in a later snapshot require another call.
    """
    from .event_order import ordered_events
    active = {}
    for event in ordered_events(events):
        payload = event['payload']
        writer = payload.get('_writer', {})
        identity = writer.get('id', 'single')
        if event['type'] == 'run_start':
            if not writer:
                active.clear()
            control = payload.get('control')
            if control is not None:
                if (not isinstance(control, dict)
                        or not isinstance(control.get('instance_id'), str)
                        or not control['instance_id']
                        or not isinstance(control.get('stop_supported'), bool)):
                    raise ValueError('Run has invalid cooperative stop metadata')
                if control['stop_supported']:
                    active[identity] = dict(control)
        elif event['type'] in {'run_finish', 'writer_finish'}:
            active.pop(identity, None)
    return sorted(active.values(), key=lambda control: control['instance_id'])


def matches(event, *, project, entity, run_id, instance_id):
    """Require the complete scope and action before considering a control event."""
    payload = event.get("payload")
    return (event.get("type") == REQUEST_TYPE and event.get("project") == project
            and event.get("entity", "") == entity and event.get("run_id") == run_id
            and isinstance(event.get("event_id"), str) and bool(event["event_id"])
            and isinstance(payload, dict) and payload.get("action") == "stop"
            and payload.get("instance_id") == instance_id)


class MemoryControlReader:
    """Follow only appended events in a MemorySink, using the native contract."""
    def __init__(self, store, **scope):
        self.store, self.scope = store, scope
        self.offset = len(store.events)

    def poll(self):
        end = min(len(self.store.events), self.offset + 4000)
        events = list(self.store.events[self.offset:end])
        self.offset = end
        return [event for event in events if matches(event, **self.scope)]


class FoundryControlReader:
    """Follow sparse stream offsets with bounded request and recovery work.

    Main callers: RunStopChecker. At most four live pages are read per poll,
    with round-robin partition service. SQL recovery is a single scoped page;
    it can find a stop that left live retention while a process was suspended.
    A dedicated store uses short HTTP timeouts without mutating the uploader.
    """
    _page_size = 1000
    _max_pages = 4

    def __init__(self, store, **scope):
        from .foundry import FoundryStore
        self.store = FoundryStore({**store.config, "timeout": min(store.config.get("timeout", 60), 5)}, token=store.token)
        self.scope = scope
        self.offsets = self._ends()
        self._turn = 0
        self._last_archive = time.monotonic()
        self._last_poll = self._last_archive
        self._recover = False

    def _ends(self):
        store = self.store
        values = store.request(store.stream_path + "/getEndOffsets?" + store._stream_query())
        return {key: int(value) for key, value in values.items()}

    def poll(self):
        now = time.monotonic()
        recovery = self._recover or now-self._last_poll > ARCHIVE_INTERVAL or now-self._last_archive >= ARCHIVE_INTERVAL
        self._last_poll = now
        live_error = None
        try:
            result = self._live()
        except Exception as error:
            live_error = error
            result = []
            self._recover = True
        # Recovery must still run when an expired live offset keeps failing.
        # The failing cursor is retained; no unseen records are acknowledged.
        if not result and recovery:
            try:
                result = self._archive()
                self._last_archive = time.monotonic()
                self._recover = live_error is not None
            except Exception:
                self._recover = True
                raise
        if live_error is not None and not result:
            raise live_error
        return result

    def _live(self):
        store = self.store
        ends = self._ends()
        active = sorted(key for key, end in ends.items() if self.offsets.get(key, 0) < end)
        if not active:
            return []
        turn = self._turn % len(active)
        active = active[turn:] + active[:turn]
        self._turn += 1
        result = []
        for _ in range(self._max_pages):
            if not active:
                break
            partition = active.pop(0)
            offset = self.offsets.get(partition, 0)
            rows = store.request(store.stream_path + "/getRecords?" + store._stream_query(
                partitionId=partition, limit=self._page_size, startOffset=str(offset)))
            if not rows:
                # A stale cursor may have no live rows. Periodic SQL recovery
                # handles retention; do not invent an acknowledgement here.
                continue
            next_offset = max(int(row["offset"]) for row in rows) + 1
            if next_offset <= offset:
                raise RuntimeError("Foundry control stream returned non-advancing offsets")
            for row in rows:
                if offset <= int(row["offset"]) < ends[partition]:
                    record = row["value"]
                    if record.get("event_type") == REQUEST_TYPE:
                        event = store.record_to_event(record)
                        if matches(event, **self.scope):
                            result.append(event)
            # Keep records beyond the captured end available to the next poll.
            self.offsets[partition] = min(next_offset, ends[partition])
            if self.offsets[partition] < ends[partition]:
                active.append(partition)
            if result:
                break
        return result

    def _archive(self):
        store = self.store
        filters = [f"{column} = {store._sql_text(self.scope[column])}" for column in ("project", "entity", "run_id")]
        filters.append(f"event_type = {store._sql_text(REQUEST_TYPE)}")
        # The UUID is a literal substring, not an assumed SQL JSON extension.
        # Exact JSON action/instance matching still happens after decoding.
        filters.append(f"payload LIKE {store._sql_text('%' + self.scope['instance_id'] + '%')}")
        sql = (f"SELECT * FROM `{store.stream_rid}` WHERE " + " AND ".join(filters)
               + " ORDER BY timestamp DESC, event_id DESC LIMIT 1000")
        rows = store._query_csv(sql, timeout=10, retry=False)
        return [event for row in rows if matches(event := store.record_to_event(row), **self.scope)]
