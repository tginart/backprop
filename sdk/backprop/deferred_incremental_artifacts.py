"""Resolve pending artifact manifests without rewriting their source journal.

Architecture: immutable delta or distributed intent in the original journal ->
first online sync selects the visible base or contributed parts -> atomic shared file and local cache -> projected event and
reference stream -> ordinary uploads/acknowledgements. A per-artifact OS lock
serializes local preparation; The backend claim chooses one winner across machines.
Retry reuses the cached base, timestamps
and content version. Main callers: Journal, artifact handoff and artifact reads.
No pending delta is published as a complete remote artifact.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from .artifact_events import iter_artifact_events, manifest_identity
from .deferred_branch import _atomic_json
from .event_order import ordered_events
from .process_lock import exclusive_lock

MARKER = '_backprop_incremental'
FOLDER = 'incremental-artifacts'


def pending(payload):
    return MARKER in payload or '_backprop_distributed' in payload


def _key(entity, project, identity):
    return hashlib.sha256(json.dumps([entity,project,identity],separators=(',',':')).encode()).hexdigest()


def _path(directory, entity, project, identity, *, namespace=None):
    folder = Path(directory) / FOLDER
    if namespace:
        folder = folder / namespace
    return folder / (_key(entity, project, identity) + '.json')


def _identity(event):
    return event.get('entity',''),event['project'],event['payload']['artifact_id']


def _fingerprint(event):
    return hashlib.sha256(manifest_identity(event).encode()).hexdigest()


def _load(directory, event, *, namespace=None):
    """Validate a saved materialization against this immutable delta."""
    path=_path(directory,*_identity(event),namespace=namespace)
    if not path.exists():return None
    value=json.loads(path.read_text())
    if value.get('source_fingerprint')!=_fingerprint(event):
        raise ValueError('Incremental artifact materialization conflicts with its original delta')
    if value.get('shared_resolution'):
        _validate_resolution(value,event)
    return value


def _full_payload(delta, base):
    """Overlay new paths and calculate the final native content version.

    Main caller: _freeze. Base file references remain immutable; unchanged blobs
    need no download/re-upload. Metadata and description belong to the delta.
    """
    payload=copy.deepcopy(delta)
    payload.pop(MARKER,None)
    payload.pop('_backprop_distributed',None)
    files={ref['name']:copy.deepcopy(ref) for ref in base.get('files',[])}
    files.update({ref['name']:copy.deepcopy(ref) for ref in payload.get('files',[])})
    payload['files']=[files[name] for name in sorted(files)]
    if base:payload['incremental_base_id']=base['artifact_id']
    from .artifacts import Artifact
    value=Artifact._from_manifest(payload,None)
    value._logged=False;value._stored_digest=None;value._version=None
    payload['digest']=value._content_digest(payload.get('tags',[]))
    payload['version']='sha256-'+payload['digest']
    return payload


def _validate_resolution(saved,event):
    """Reject conflicting source deltas before accepting another writer's choice.

    Main caller: _freeze. Full manifests may contain additional inherited files,
    but the original scoped delta and provisional digest must agree exactly.
    """
    if (saved.get('source_fingerprint')!=_fingerprint(event)
            or saved.get('source_digest')!=event['payload']['digest']
            or saved.get('identity')!=list(_identity(event))):
        raise ValueError('Shared incremental artifact resolution conflicts with its original delta')
    payload=saved['payload']
    if '_backprop_distributed' in event['payload'] and saved.get('distributed_closure'):
        from .distributed_artifact_closure import validate_binding
        validate_binding(saved,event)
    elif payload['artifact_id']!=event['payload']['artifact_id']:
        raise ValueError('Shared incremental artifact resolution has a conflicting artifact ID')
    if pending(payload):
        raise ValueError('Shared incremental artifact resolution is not a complete matching manifest')
    _validate_content(saved)


def _validate_content(saved):
    """Validate cached full manifests before replay or live-handle refresh."""
    payload=saved['payload']
    if any('_backprop_local_file' in ref for ref in payload.get('files',[])):
        raise ValueError('Shared incremental artifact resolution contains a private local file')
    checked=_full_payload(payload,{})
    if (checked['digest'],checked['version'])!=(payload['digest'],payload['version']):
        raise ValueError('Shared incremental artifact resolution failed its content digest check')
    datetime.fromisoformat(saved['origin'])


def _candidate(event,visible):
    """Choose a visible predecessor only when no shared resolution exists yet.

    Main caller: _freeze. Re-logged older versions retain their creation order;
    new paths replace inherited paths. The candidate is not yet authoritative.
    """
    if '_backprop_distributed' in event['payload']:
        from .distributed_artifact_resolution import candidate
        return candidate(event,visible)
    entity,project,identity=_identity(event);delta=event['payload']
    candidates=[e for e in iter_artifact_events(ordered_events(visible))
        if e.get('entity','')==entity and e['project']==project
        and e['payload']['name']==delta['name'] and e['payload']['artifact_type']==delta['artifact_type']
        and e['payload']['artifact_id']!=identity and not pending(e['payload'])]
    first={}
    for candidate in candidates:first.setdefault(candidate['payload']['artifact_id'],candidate)
    base=max(first.values(),key=lambda e:(datetime.fromisoformat(e['payload'].get('created_at',e['timestamp']).replace('Z','+00:00')),e['payload']['artifact_id'])) if first else None
    origin=datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
    if base:origin=max(origin,datetime.fromisoformat(base['timestamp'].replace('Z','+00:00'))+timedelta(microseconds=1))
    payload=_full_payload(delta,base['payload'] if base else {})
    payload['created_at']=origin.astimezone(timezone.utc).isoformat(timespec='microseconds')
    return {'source_fingerprint':_fingerprint(event),'source_digest':delta['digest'],
            'identity':list(_identity(event)),'origin':payload['created_at'],'payload':payload}


def _freeze(journal,event,visible):
    """Freeze one shared result before any journal publishes its artifact event.

    Main caller: prepare. The local lock protects the cache; an atomic APPEND in
    the existing file dataset coordinates different machines and private caches.
    Candidate files upload first, so a winning resolution contains no private
    paths. A lost claim response or local process crash reuses the remote winner.
    """
    namespace = getattr(journal.sink, 'ack_namespace', None)
    path=_path(journal.directory,*_identity(event),namespace=namespace);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as lock,exclusive_lock(lock):
        saved=_load(journal.directory,event,namespace=namespace)
        if saved is not None and saved.get('shared_resolution'):
            return saved
        key=_key(*_identity(event))
        winner=journal.sink.read_artifact_resolution(key)
        if winner is None:
            # Older local caches retain their already frozen content when first
            # promoted to shared storage. New candidates consult visible events.
            if '_backprop_distributed' in event['payload']:
                from .distributed_artifact_closure import select
                candidate=select(journal,event,visible)
            else:
                candidate=copy.deepcopy(saved) if saved is not None else _candidate(event,visible())
            candidate['identity']=list(_identity(event))
            candidate['payload']=journal._resolve(candidate['payload'])
            winner=journal.sink.claim_artifact_resolution(key,candidate)
        _validate_resolution(winner,event)
        winner={**winner,'shared_resolution':True}
        _atomic_json(path,winner)
        return winner


def _reference_fields(value, resolved, scope):
    """Resolve one scoped reference while preserving caller-owned labels.

    Main callers: nested config values and artifact input/edit event projection.
    Input relationships have no _type tag, so their event kind is authoritative.
    """
    key=(value.get('source_entity',scope[0]),value.get('source_project',scope[1]),value.get('artifact_id'))
    if key not in resolved:return {}
    payload=resolved[key]['payload'];name=payload['name']+':'+payload['version']
    return dict(artifact_id=payload['artifact_id'],name=name,version=payload['version'],digest=payload['digest'],
                qualified_name='/'.join(part for part in [key[0],key[1],name] if part))


def _references(value, resolved, scope):
    if isinstance(value,list):return [_references(item,resolved,scope) for item in value]
    if not isinstance(value,dict):return value
    result={key:_references(item,resolved,scope) for key,item in value.items()}
    if value.get('_type')=='artifact':
        result.update(_reference_fields(value,resolved,scope))
    return result


def _project(events, resolved):
    """Rewrite only unpublished identities/references and retain event ordering.

    Main callers: prepare and cached_events. The suffix shifts after a base
    whose clock is newer than the offline journal; relative timing is retained.
    Every original event still maps to exactly one upload event/cursor position.
    """
    result=[];shift=timedelta(0);previous=None
    for event in events:
        payload=event.get('payload',{});stamp=datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
        saved=resolved.get(_identity(event)) if event['type']=='artifact' and pending(payload) else None
        if saved is not None:
            shift=max(shift,datetime.fromisoformat(saved['origin'])-stamp)
            payload=copy.deepcopy(saved['payload'])
            # Writer ordering belongs to this event, not the winning manifest.
            payload.pop('_writer',None)
            if '_writer' in event['payload']:
                payload['_writer']=copy.deepcopy(event['payload']['_writer'])
        stamp=stamp+shift
        if shift>timedelta(0) and previous is not None:stamp=max(stamp,previous+timedelta(microseconds=1))
        previous=stamp
        original=datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
        timestamp=event['timestamp'] if stamp==original else stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
        scope=event.get('entity',''),event['project']
        payload=_references(payload,resolved,scope)
        if event['type']=='artifact_use':
            # Input events also carry a versioned name, even without _type.
            fields=_reference_fields(payload,resolved,scope)
            payload.update({key:value for key,value in fields.items() if key in payload})
        elif event['type']=='artifact_update':
            # A relogged pending output can carry edits queued behind it. Its
            # name remains the collection name, never a versioned reference.
            fields=_reference_fields(payload,resolved,scope)
            if fields:payload['artifact_id']=fields['artifact_id']
        result.append({**event,'timestamp':timestamp,'payload':payload})
    return result


def cached_events(directory, events, *, namespace=None):
    """
    Purpose: Project artifact records from one installation's durable cache.
    Input: The original records and the selected acknowledgement namespace.
    Output: Materialized records without credentials or remote requests.
    Note: The default namespace preserves existing Foundry cache behavior.
    """
    if not (Path(directory)/FOLDER).exists():return events
    resolved={}
    for event in events:
        if event['type']=='artifact' and pending(event.get('payload',{})):
            saved=_load(directory,event,namespace=namespace)
            if saved is not None:resolved[_identity(event)]=saved
    return _project(events,resolved) if resolved else events


def prepare(journal, events, *, through_artifact=None):
    """Resolve pending deltas in order, reusing earlier local outputs as bases.

    Main callers: Journal.flush and artifact-only handoff. No original rows are
    changed or acknowledged here. Remote reads occur once per scope and only
    while selecting an uncached base. Failures leave the original queue intact.
    """
    resolved={};remote={};prefix=[]
    namespace = getattr(journal.sink, 'ack_namespace', None)
    for event in events:
        if event['type']=='artifact' and pending(event.get('payload',{})):
            saved=_load(journal.directory,event,namespace=namespace)
            if saved is None or not saved.get('shared_resolution'):
                def visible():
                    scope=event.get('entity',''),event['project']
                    if scope not in remote:
                        remote[scope]=journal.sink.read_events(entity=scope[0],project=scope[1])
                    prior=_project(prefix,resolved)
                    return list({e['event_id']:e for e in [*remote[scope],*prior]}.values())
                saved=_freeze(journal,event,visible)
            resolved[_identity(event)]=saved
        prefix.append(event)
        if through_artifact is not None and event['type']=='artifact' and event['payload']['artifact_id']==through_artifact:break
    return _project(events,resolved) if resolved else events


def prepare_source(artifact, directory, sink):
    """Freeze an offline artifact before it is copied into an online journal."""
    from .transport import Journal, read_journal
    journal=Journal(directory,sink)
    events=read_journal(directory,project_artifacts=False)
    prepare(journal,events,through_artifact=artifact._id)
    refresh(artifact,sink=sink)


def refresh(artifact, *, sink=None):
    """Refresh a pending handle after this or another process froze its base.

    Main callers: artifact state/version/manifest access. Existing delta entry
    objects retain identity. Mutable descriptions stay owned by the caller.
    """
    if not (getattr(artifact,'_incremental_pending',False) or getattr(artifact,'_distributed_pending',False)) or not artifact._logged:return
    from .config_artifacts import JournalArtifactStore
    owner=getattr(artifact,'_owner',None);journal=getattr(owner,'_journal',None)
    directory=journal.directory if journal else artifact._store.directory if isinstance(artifact._store,JournalArtifactStore) else None
    if directory is None:return
    if sink is None:
        sink = journal.sink if journal else artifact._store.remote
    namespace = getattr(sink, 'ack_namespace', None)
    path=_path(directory,artifact.entity,artifact.project,artifact._id,namespace=namespace)
    if not path.exists():return
    from .transport import read_journal
    source=next((event for event in read_journal(directory,project_artifacts=False)
        if event['type']=='artifact' and _identity(event)==(artifact.entity,artifact.project,artifact._id)),None)
    if source is None:
        raise ValueError('Materialized artifact has no matching durable source intent')
    saved=_load(directory,source,namespace=namespace);payload=saved['payload']
    if saved['source_digest']!=artifact._stored_digest or saved['identity']!=[artifact.entity,artifact.project,artifact._id]:
        raise ValueError('Materialized artifact handle conflicts with its queued delta')
    from dataclasses import replace
    from .artifacts import Artifact
    full=Artifact._from_manifest(payload,artifact._store,artifact.entity,artifact.project,artifact._run_id)
    for name,entry in full._entries.items():
        if name not in artifact._entries:
            ref=entry._reference
            local=str(Path(directory)/ref['_backprop_local_file']) if '_backprop_local_file' in ref else None
            artifact._entries[name]=replace(entry,_parent=artifact,local_path=local)
    # A competing finalizer may have selected another immutable ID. Preserve
    # the caller's entry objects while giving them the canonical provenance.
    for name,entry in artifact._entries.items():
        reference=full._entries[name]
        for field in ['birth_artifact_id','ref','_reference']:
            object.__setattr__(entry,field,getattr(reference,field))
    artifact._id=payload['artifact_id']
    if saved.get('distributed_closure'):
        # The selected finalizer owns the published description. Preserve edits
        # made locally after queuing; save() must compare those with the actual
        # winning description, not with the losing caller's original draft.
        before=artifact._saved_descriptive
        for field in ('metadata','description','tags'):
            current=getattr(artifact,field)
            canonical=copy.deepcopy(payload.get(field,{} if field=='metadata' else [] if field=='tags' else None))
            if current==before[field]:
                if field=='tags':artifact._tags=canonical
                else:setattr(artifact,field,canonical)
        if artifact._aliases==artifact._saved_aliases:
            artifact._aliases=list(payload.get('aliases',[]))
        artifact._saved_descriptive=copy.deepcopy({field:payload.get(field) for field in ('metadata','description','tags')})
        artifact._saved_aliases=list(payload.get('aliases',[]))
    artifact._version=payload['version'];artifact._stored_digest=payload['digest']
    artifact._created_at=payload.get('created_at');artifact._incremental_pending=False;artifact._distributed_pending=False


def restored_publication_status(artifact):
    """Read the acknowledgment of a restored artifact's containing envelope.

    Main callers: config hydration and Artifact.state. None means this object
    has no publication in that local journal, as with a remote-only snapshot.
    False means a local draft, even when its manifest contains no files. True
    requires durable acknowledgment. Value-owned artifacts use their enclosing
    history/summary event; materialized branches use the prepared queue cursor.
    """
    from .config_artifacts import JournalArtifactStore
    if not isinstance(artifact._store, JournalArtifactStore):
        return None
    from .deferred_branch import read_local_branch
    from .transport import Journal
    directory=artifact._store.directory
    if not (directory/'events.jsonl').is_file():
        return None
    branch=read_local_branch(directory)
    journal=Journal(directory,artifact._store.remote,_branch_prepared=branch is not None)
    acknowledged=journal.acknowledged_count()
    if branch is None:
        with journal.event_path.open(encoding='utf-8') as source:
            events=[json.loads(line) for line in source if line.strip()]
    else:
        events=branch
    namespace = getattr(artifact._store.remote, 'ack_namespace', None)
    for position,event in enumerate(cached_events(directory,events,namespace=namespace),1):
        for manifest in iter_artifact_events([event]):
            if (manifest.get('entity','')==artifact.entity and manifest['project']==artifact.project
                    and manifest['payload'].get('artifact_id')==artifact._id):
                return acknowledged>=position
    return None


def restored_acknowledged(artifact):
    """Observe a restored local object's durable publication boundary.

    Main caller: Artifact.state after hydration. Merely preparing a manifest
    does not acknowledge it; another process's later sync is observed here.
    """
    return restored_publication_status(artifact) is True
