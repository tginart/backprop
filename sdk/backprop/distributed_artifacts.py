"""Collect distributed outputs and journal finalization through existing storage.

Architecture: public validation -> immutable part -> durable finalization intent
-> normal sync freezes a visible union in the shared resolution file -> publish.
Offline and online calls use the same intent. The designated primary closes the
visible set; late parts remain available for the next version. No scheduler or
all-worker barrier is implied. Main callers: Run.upsert_artifact/finish_artifact.
"""
from .artifacts import Artifact, _validate_metadata
from .artifact_logging import prepare, validate_output
from .artifact_updates import validate_aliases

MARKER = '_backprop_distributed'


def group_id(run, value, operation):
    """Resolve the group default, preserving the pinned empty-string value.

    Main callers: contribution and finish. None alone selects the run group.
    Missing-group errors occur before aliases or input preparation.
    """
    if value is None and run._settings.run_group is None:
        raise TypeError(f'Cannot {operation} artifact unless run is in a group or distributed_id is provided')
    return run._settings.run_group if value is None else value


def transport_group(value):
    """Match the pinned publisher's UTF-8 group encoding without protobuf.

    Main callers: contribution and finish recovery. The public descriptor keeps
    the original value. False values encode an empty group; truthy text or bytes
    encode UTF-8, while other truthy values raise the transport's TypeError.
    This normalization happens after public preparation and ownership checks.
    """
    if not value:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8')
    if isinstance(value, str):
        return value.encode('utf-8').decode('utf-8')
    raise TypeError('bad argument type for built-in operation')


def _snapshot(artifact):
    """Reuse a logged object's recorded files without requiring commitment.

    Main callers: contribution and finalization. Public new_draft requires a
    committed object; publication also accepts queued objects. Their existing
    immutable entries can be copied into a fresh internal snapshot instead.
    """
    from .artifacts import _thaw, ArtifactDigestAlgorithm
    draft=Artifact._new_internal(artifact._name,artifact.type,description=artifact.description,
        metadata=_thaw(artifact.metadata),digest_algorithm='XXH128' if artifact.digest_algorithm is ArtifactDigestAlgorithm.MANIFEST_XXH128 else 'MD5')
    for entry in artifact._entries.values():draft._copy_entry(entry,ref=entry.ref)
    return draft


def contribution(run, artifact_or_path, name=None, type=None, aliases=None, distributed_id=None):
    """Persist one immutable contribution before any network attempt.

    Main caller: Run.upsert_artifact, and finish's own contribution. Reconstructed
    retries find unacknowledged operations on disk; a new acknowledged operation
    may contribute identical content again under the same distributed group.
    """
    artifact,aliases,distributed_id=_prepare_contribution(run,artifact_or_path,name,type,aliases,distributed_id)
    return _record_contribution(run,artifact,aliases,distributed_id)


def _prepare_contribution(run,artifact_or_path,name,type,aliases,distributed_id):
    """Validate each call before recovery or deduplication can return early.

    Main callers: contribution and finish. Preparation preserves pinned argument
    validation order and public descriptor side effects even for repeat calls.
    """
    distributed_id=group_id(run,distributed_id,'upsert')
    aliases=None if aliases is None else validate_aliases(aliases)
    artifact=prepare(run,artifact_or_path,name,type)
    artifact.metadata=_validate_metadata({**artifact.metadata})
    artifact.distributed_id=distributed_id
    validate_output(artifact,run)
    return artifact,aliases,transport_group(distributed_id)


def _record_contribution(run,artifact,aliases,distributed_id):
    """Append new intent, or recover the same unmodified pending contribution.

    Main callers: contribution and finish after argument validation. Changed
    descriptive values need a new part. A finished round also needs a fresh part
    even offline; its old part is already assigned to a finalization intent.
    """
    artifact._contribution_committed=artifact.state=='COMMITTED'
    pending=getattr(artifact,'_pending_contribution',None)
    if pending is not None and pending!=(run.sync_dir,distributed_id):
        artifact._pending_contribution_journal.flush()
        artifact._pending_contribution=None
        artifact._pending_contribution_journal=None
    from .artifact_operations import operation_id, recover_local, fingerprint
    # Existing objects retain their immutable files and use the collection name,
    # independent of the version suffix shown by the public name property.
    if artifact._logged:
        snapshot=_snapshot(artifact)
    else:snapshot=artifact
    manifest=snapshot._prepare_log(run,aliases)
    scope=(run.sync_dir,distributed_id)
    value=fingerprint(run,distributed_id,manifest)
    finished=getattr(artifact,'_finalized_operation',None)==getattr(artifact,'_operation_id',None) and getattr(artifact,'_operation_id',None) is not None
    retained=(getattr(artifact,'_operation_id',None) if getattr(artifact,'_operation_scope',None)==scope
        and getattr(artifact,'_operation_fingerprint',None)==value
        and not artifact._contribution_committed and not finished else None)
    if finished:
        import uuid
        identity=uuid.uuid4().hex
    else:identity=retained or operation_id(run,distributed_id,manifest)
    artifact._operation_scope=scope
    artifact._operation_id=identity
    artifact._operation_fingerprint=value
    original=recover_local(run,identity,'artifact_part')
    from .artifact_contributions import bind
    if original is not None:
        bind(artifact,run,identity)
        artifact._pending_contribution=None
        artifact._pending_contribution_journal=None
        return artifact
    run._emit('artifact_part',{'distributed_id':distributed_id,'part_id':identity,'operation_id':identity,
        'operation_fingerprint':artifact._operation_fingerprint,'manifest':manifest})
    artifact._pending_contribution=(run.sync_dir,distributed_id)
    artifact._pending_contribution_journal=run._journal
    bind(artifact,run,identity)
    run.flush();artifact._pending_contribution=None;artifact._pending_contribution_journal=None
    return artifact


def _accept(artifact,run,payload):
    """Attach a recovered final to the supplied object, preserving entry handles.

    Main caller: finish recovery. Unlike a new read object, a public finish must
    return its argument. Existing entry handles keep their parent and gain the
    recovered immutable references; additional contributed entries are attached.
    """
    from dataclasses import replace
    full=Artifact._from_manifest(payload,run._sink,run.entity,run.project,run.id)
    for name,entry in full._entries.items():
        if name in artifact._entries:
            target=artifact._entries[name]
            for field in ['digest','size','sha256','ref','birth_artifact_id','extra','_reference']:
                object.__setattr__(target,field,getattr(entry,field))
            object.__setattr__(target,'local_path',None)
        else:artifact._entries[name]=replace(entry,_parent=artifact)
    artifact._id=full._id
    artifact._distributed_pending=bool(payload.get(MARKER))
    artifact._commit_log(run,payload)
    return artifact


def finish(run, artifact_or_path, name=None, type=None, aliases=None, distributed_id=None):
    """Queue a finalization intent and return the caller's artifact object.

    Main caller: Run.finish_artifact. Sync owns visible-part selection and its
    immutable resolution, including offline calls and failure after acceptance.
    The original journal and event IDs survive retries and process restart.
    """
    public_group=group_id(run,distributed_id,'finish')
    if run._shared and not run._settings.x_primary:
        raise ValueError('Only the primary shared writer can finalize an artifact')
    artifact,aliases,distributed_id=_prepare_contribution(run,artifact_or_path,name,type,aliases,public_group)
    import json
    from .artifact_updates import logging_aliases
    # Immutable files cannot change after preparation. Keep the original request
    # separate from the expanded union so a lost response can retry that intent.
    request=(run.sync_dir,distributed_id,json.dumps({'name':artifact._name,'type':artifact.type,
        'metadata':artifact.metadata,'description':artifact.description,'aliases':logging_aliases(aliases)},sort_keys=True))
    pending=getattr(artifact,'_finish_pending',None)
    if pending is not None:
        original_request,journal=pending
        journal.flush()
        artifact._finish_pending=None
        if original_request==request:return artifact
    _record_contribution(run,artifact,aliases,distributed_id)
    from .artifact_operations import recover_local
    target=_snapshot(artifact) if artifact._contribution_committed else artifact
    original=recover_local(run,artifact._operation_id,'artifact')
    if original is not None:
        _accept(target,run,original['payload'])
        artifact.distributed_id=public_group
        if target is not artifact:artifact._distributed_result=target
        artifact._finalized_operation=artifact._operation_id
        return artifact
    # A committed input is a source of files; finalization records another output
    # using a fresh scoped identity. Its already committed public argument keeps
    # its original ID, state and contents, as upstream wait() does not reload it.
    snapshot=_snapshot(target) if target._logged else target
    payload=snapshot._prepare_log(run,aliases)
    payload.update(distributed_id=distributed_id,operation_id=artifact._operation_id,
        operation_fingerprint=artifact._operation_fingerprint,**{MARKER:{'version':1}})
    if run._sink is not None:
        # Reject already-visible conflicts before queuing a finalization intent.
        # Sync repeats selection under its durable resolution lock; later
        # conflicts remain explicit rather than publishing an incomplete union.
        from .distributed_artifact_closure import preflight
        from .transport import read_journal
        preview={'schema_version':1,'event_id':'finalization-preview','type':'artifact',
            'run_id':run.id,'entity':run.entity,'project':run.project,
            'timestamp':payload['created_at'],'payload':payload}
        visible={e['event_id']:e for e in [*run._sink.read_events(entity=run.entity,project=run.project),*read_journal(run.sync_dir,sink=run._sink)]}
        preflight(run._sink,preview,list(visible.values()))
    run._emit('artifact',payload)
    target._id=payload['artifact_id'];target._distributed_pending=True;target._committed=False
    target._commit_log(run,payload)
    artifact.distributed_id=public_group
    if target is not artifact:artifact._distributed_result=target
    artifact._finalized_operation=artifact._operation_id
    artifact._finish_pending=(request,run._journal)
    run.flush()
    artifact._finish_pending=None
    return artifact
