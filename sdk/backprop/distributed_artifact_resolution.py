"""Select the visible unconsumed parts of a distributed finalization intent.

Architecture: scoped ordered events -> consumed IDs -> visible part union ->
complete immutable manifest. The existing deferred-artifact resolver freezes
this candidate in local and shared storage before publication. No public read
can confuse a pending intent with a completed remote artifact.
Main caller: deferred_incremental_artifacts._candidate.
"""
import copy
from datetime import datetime,timedelta,timezone
from .event_order import ordered_events


def completed_parts(event, visible):
    """Retain completed legacy event consumption when bootstrapping a group.

    Main callers: candidate and the durable closure selector. New closure files
    retain these IDs so later delayed queries cannot make them available again.
    """
    from .distributed_artifacts import MARKER
    delta=event['payload']
    return {identity for e in visible if e['type']=='artifact' and MARKER not in e['payload'] and not e['payload'].get('distributed_closure_key')
        and (e.get('entity',''),e['project'])==(event.get('entity',''),event['project'])
        and e['payload'].get('distributed_id')==delta['distributed_id'] and e['payload']['name']==delta['name']
        for identity in e['payload'].get('contribution_ids',[])}


def candidate(event,visible,*,consumed=()):
    """Build a deterministic visible union without mutating the queued intent.

    Main caller: deferred artifact preparation. Same-path equal bytes merge;
    different bytes or artifact types fail before a complete manifest publishes.
    Previously consumed parts stay in their immutable versions. The current
    finalizer's part is available from its own preceding journal envelope.
    """
    from .deferred_incremental_artifacts import _full_payload,_fingerprint,_identity
    from .distributed_artifacts import MARKER
    from .artifact_events import manifest_identity
    delta=event['payload'];scope=_identity(event)[:2]
    events=ordered_events([e for e in visible if (e.get('entity',''),e['project'])==scope])
    consumed=set(consumed)|completed_parts(event,events)
    parts={}
    for item in events:
        p=item['payload']
        if (item['type']=='artifact_part' and p['distributed_id']==delta['distributed_id']
                and p['manifest']['name']==delta['name'] and p['part_id'] not in consumed):
            if p['part_id'] in parts and parts[p['part_id']]['payload']['manifest']!=p['manifest']:
                raise ValueError('Conflicting distributed artifact contribution identity')
            parts[p['part_id']]=item
    files={}
    for part in parts.values():
        manifest=part['payload']['manifest']
        if manifest['artifact_type']!=delta['artifact_type']:
            raise ValueError('Distributed artifact types disagree')
        for ref in manifest['files']:
            old=files.get(ref['name'])
            if old is not None and manifest_identity({**event,'payload':{'files':[old]}})!=manifest_identity({**event,'payload':{'files':[ref]}}):
                raise ValueError(f"Conflicting distributed artifact path: {ref['name']}")
            selected=copy.deepcopy(ref)
            # A contribution draft is not a published artifact. Newly born files
            # belong to the completed union; committed-source ancestry survives.
            if selected.get('birth_artifact_id')==manifest['artifact_id']:
                selected['birth_artifact_id']=delta['artifact_id']
            files.setdefault(ref['name'],selected)
    if delta['operation_id'] not in parts:
        raise ValueError('The finalizer contribution is absent or already consumed; retry after its publication is visible')
    payload=copy.deepcopy(delta);payload.pop(MARKER);payload['files']=[files[name] for name in sorted(files)]
    payload['contribution_ids']=list(parts)
    # Each accepted contribution requests aliases on the eventual artifact.
    # Freeze their union with the selected files so a retry cannot acquire names
    # from late parts. Explicit post-commit removals remain artifact_update events.
    payload['aliases']=sorted({alias for part in parts.values()
        for alias in part['payload']['manifest'].get('aliases',[])})
    payload=_full_payload(payload,{})
    origin=datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
    for part in parts.values():origin=max(origin,datetime.fromisoformat(part['timestamp'].replace('Z','+00:00'))+timedelta(microseconds=1))
    payload['created_at']=origin.astimezone(timezone.utc).isoformat(timespec='microseconds')
    return {'source_fingerprint':_fingerprint(event),'source_digest':delta['digest'],
        'identity':list(_identity(event)),'origin':payload['created_at'],'payload':payload}
