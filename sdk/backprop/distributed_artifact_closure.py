"""Persist group consumption independently of delayed analytical queries.

Architecture: entity/project/collection/group -> immutable linked closure files
in the existing artifact-resolution store -> first APPEND wins -> bind each
finalization intent to its canonical manifest. Each record consumes one visible
set of parts. Later parts remain eligible for the next record. Files upload before
claiming; retries after process exit or a lost response read the same winner.
Main callers: deferred artifact preparation and online conflict preflight.
No mutable coordinator, worker barrier or additional Foundry resource is needed.
"""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

KIND = 'distributed-artifact-closure'


def scope(event):
    payload = event['payload']
    return [event.get('entity', ''), event['project'], payload['name'], payload['distributed_id']]


def slot(group, previous):
    """Give each append position one deterministic, domain-separated file key."""
    return hashlib.sha256(json.dumps([KIND, 1, group, previous], separators=(',', ':'),
        sort_keys=True, allow_nan=False).encode()).hexdigest()


def _validate(record, event, previous, consumed):
    """Reject malformed, cross-group or overlapping records before accepting them.

    Main caller: walk. The completed payload and creation time are authoritative;
    an existing immutable slot cannot silently be replaced or skipped on failure.
    """
    from .deferred_incremental_artifacts import _validate_content, pending
    if (record.get('kind') != KIND or record.get('version') != 1
            or record.get('scope') != scope(event) or record.get('previous') != previous):
        raise ValueError('Distributed artifact closure has conflicting scope or predecessor')
    resolution = record['resolution'];payload = resolution['payload']
    if (resolution.get('identity') != scope(event)[:2]+[payload['artifact_id']]
            or payload.get('name') != event['payload']['name']
            or payload.get('distributed_id') != event['payload']['distributed_id']
            or payload.get('artifact_type') != event['payload']['artifact_type']
            or payload.get('distributed_closure_key') != slot(scope(event), previous) or pending(payload)):
        raise ValueError('Distributed artifact closure has a conflicting immutable manifest')
    parts = payload.get('contribution_ids', [])
    prior = record.get('consumed_before', [])
    if (not parts or any(not isinstance(value, str) or not value for value in parts+prior)
            or len(set(parts)) != len(parts) or len(set(prior)) != len(prior)
            or not consumed.issubset(prior) or set(parts).intersection(prior)
            or payload.get('operation_id') not in parts):
        raise ValueError('Distributed artifact closure contains conflicting consumed part IDs')
    if resolution.get('origin') != payload.get('created_at'):
        raise ValueError('Distributed artifact closure has conflicting creation times')
    _validate_content(resolution)


def walk(sink, event):
    """Read the committed chain, or find a prior closure containing this part.

    Main callers: select and preflight. Query visibility is never used to forget
    consumed IDs. A transiently unreadable tail may be claimed again, but APPEND
    then returns its real winner; it cannot create a second branch at that slot.
    """
    previous=None;consumed=set();last=None
    while True:
        key=slot(scope(event),previous)
        record=sink.read_artifact_resolution(key)
        if record is None:
            return key,previous,consumed,last,None
        _validate(record,event,previous,consumed)
        parts=set(record['resolution']['payload']['contribution_ids'])
        if event['payload']['operation_id'] in parts:
            return key,previous,consumed,last,record
        consumed.update(record['consumed_before']);consumed.update(parts)
        previous=key;last=record


def bind(record, key, event):
    """Bind this raw intent to the winning full manifest, including a racing ID.

    Main caller: select. Per-source fingerprints remain immutable while several
    independent finalizer intents can resolve to the same canonical artifact ID.
    The winner owns descriptive metadata and aliases for this completed version.
    """
    from .deferred_incremental_artifacts import _fingerprint, _identity
    result=copy.deepcopy(record['resolution'])
    result.update(source_fingerprint=_fingerprint(event),source_digest=event['payload']['digest'],
        identity=list(_identity(event)),distributed_closure={'key':key,'previous':record['previous'],'scope':scope(event)})
    validate_binding(result,event)
    return result


def validate_binding(saved, event):
    """Verify that a remapped result contains this intent's actual contribution.

    Main callers: bind and deferred cache validation. Merely sharing a group or
    an ID does not authorize replacing files. Different bytes or reference types
    remain explicit conflicts even when a competing finalizer already won.
    """
    from .artifact_events import manifest_identity
    marker=saved.get('distributed_closure',{});payload=saved['payload'];delta=event['payload']
    if (marker.get('scope') != scope(event)
            or marker.get('key') != slot(scope(event),marker.get('previous'))
            or payload.get('distributed_closure_key') != marker.get('key')
            or payload.get('name') != delta['name'] or payload.get('artifact_type') != delta['artifact_type']
            or payload.get('distributed_id') != delta['distributed_id']
            or delta['operation_id'] not in payload.get('contribution_ids',[])):
        raise ValueError('Distributed artifact resolution does not include its original contribution')
    files={entry['name']:entry for entry in payload['files']}
    for entry in delta['files']:
        selected=files.get(entry['name'])
        def identity(ref):
            return manifest_identity({**event,'payload':{'files':[ref]}})
        if selected is None or identity(entry)!=identity(selected):
            raise ValueError('Distributed artifact resolution changes an original contribution file')


def select(journal, event, visible):
    """Claim a complete group generation, retrying only against immutable winners.

    Main caller: deferred _freeze under its per-intent local lock. A losing
    contender advances only if its own contribution was absent from the winner.
    Claim response loss is safe: the next call walks the same durable chain.
    """
    from .distributed_artifact_resolution import candidate, completed_parts
    key,previous,consumed,last,record=walk(journal.sink,event)
    while record is None:
        events=visible()
        consumed.update(completed_parts(event,events))
        resolution=candidate(event,events,consumed=consumed)
        if last is not None:
            after=datetime.fromisoformat(last['resolution']['origin'])+timedelta(microseconds=1)
            stamp=max(after,datetime.fromisoformat(resolution['origin']))
            resolution['origin']=stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
            resolution['payload']['created_at']=resolution['origin']
        resolution['payload']['distributed_closure_key']=key
        resolution['payload']=journal._resolve(resolution['payload'])
        proposed={'kind':KIND,'version':1,'scope':scope(event),'previous':previous,
            'consumed_before':sorted(consumed),'resolution':resolution}
        record=journal.sink.claim_artifact_resolution(key,proposed)
        _validate(record,event,previous,consumed)
        parts=set(record['resolution']['payload']['contribution_ids'])
        if event['payload']['operation_id'] in parts:
            break
        consumed.update(record['consumed_before']);consumed.update(parts)
        previous=key;last=record;key=slot(scope(event),previous);record=None
    return bind(record,key,event)


def preflight(sink, event, visible):
    """Check known file conflicts before journaling, without reserving a slot.

    Main caller: online finish_artifact. Preparation repeats this read and uses
    atomic claims; preflight alone cannot decide a group generation's winner.
    """
    from .distributed_artifact_resolution import candidate
    key,previous,consumed,last,record=walk(sink,event)
    return bind(record,key,event) if record is not None else candidate(event,visible,consumed=consumed)
