"""Follow one accepted distributed part until its primary publishes the union.

Architecture: durable part journal -> bounded flush -> ordinary artifact read
view -> validate original file identities -> refresh the same public object.
Acceptance never implies commitment. Only a visible completed manifest containing
this operation can commit its handle; unseen or late parts remain pending.
Main callers: distributed contribution binding and Artifact.wait. No extra event,
service or resource is introduced. Restart recovery binds the original journal.
"""
import copy
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class ContributionHandle:
    """Keep an immutable source envelope and the journal that owns its retry."""
    journal: object
    event: dict
    descriptive: dict


def bind(artifact, run, identity):
    """Attach the durable part before an upload can fail or lose its response.

    Main caller: contribution, after append or recovered publication. A committed
    source object keeps its existing identity. Pending handles follow the latest
    caller, while retry recovery can still point at an earlier writer journal.
    """
    if artifact._contribution_committed:
        return
    from .artifact_operations import writer_journals
    from .deferred_branch import read_local_branch
    from .transport import Journal, read_journal
    for directory in writer_journals(run):
        event=next((event for event in read_journal(directory,project_artifacts=False)
            if event['type']=='artifact_part' and event['payload'].get('operation_id')==identity),None)
        if event is None:
            continue
        journal=(run._journal if Path(directory)==Path(run.sync_dir) else
            Journal(directory,run._sink,_branch_prepared=read_local_branch(directory) is not None))
        baseline={name:copy.deepcopy(getattr(artifact,name)) for name in ('metadata','description','tags')}
        artifact._contribution_handle=ContributionHandle(journal,copy.deepcopy(event),baseline)
        return
    raise ValueError('Distributed contribution has no matching durable journal entry')


def completed(handle):
    """Read a completed union without treating the part's own ACK as a commit.

    Main caller: Artifact.wait, inside its caller deadline. The same ordered,
    deduplicated public artifact view supplies metadata and aliases. A missing
    completion returns pending; conflicting identities or changed source bytes
    raise before the public handle is modified.
    """
    from .artifact_updates import artifact_views
    from .artifact_events import iter_artifact_events
    from .artifact_lineage import validate_files
    source=handle.event;part=source['payload'];manifest=part['manifest']
    scope={'entity':source.get('entity',''),'project':source['project']}
    events=handle.journal.sink.read_events(**scope)
    # Match immutable contribution scope before applying mutable collection names.
    originals=list(iter_artifact_events(events))
    matches=[]
    for event in originals:
        payload=event['payload']
        if (event.get('entity','')==scope['entity'] and event['project']==scope['project']
                and payload.get('name')==manifest['name']
                and payload.get('artifact_type')==manifest['artifact_type']
                and payload.get('distributed_id')==part['distributed_id']
                and part['operation_id'] in payload.get('contribution_ids',[])
                and not payload.get('_backprop_distributed')):
            matches.append(event)
    if not matches:
        return None
    if len({event['payload']['artifact_id'] for event in matches})!=1:
        raise ValueError('Distributed contribution belongs to conflicting completed artifacts')
    identity=matches[0]['payload']['artifact_id']
    matches=[event for event in artifact_views(events) if event['payload']['artifact_id']==identity]
    # A contributor relationship does not replace the original publisher.
    event=next(event for event in matches if event.get('source_event_type')!='artifact_part')
    validate_files(source,event)
    return event


def adopt(artifact, handle, event):
    """Refresh to the verified union while retaining returned entry objects.

    Main caller: Artifact.wait after its bounded read succeeds. The completed
    object becomes a normal remote read handle so later use/log/save operations
    read the primary's actual manifest, not the contributor's part-only journal.
    Local descriptive edits survive; their save baseline is the published union.
    """
    from .artifacts import Artifact
    payload=event['payload']
    full=Artifact._from_manifest(payload,handle.journal.sink,event.get('entity',''),event['project'],event['run_id'])
    for name,entry in full._entries.items():
        if name in artifact._entries:
            target=artifact._entries[name]
            for field in ('digest','size','sha256','ref','birth_artifact_id','extra','_reference'):
                object.__setattr__(target,field,getattr(entry,field))
            object.__setattr__(target,'local_path',None)
        else:
            artifact._entries[name]=replace(entry,_parent=artifact)
    for field in ('_id','_version','_stored_digest','_entity','_project','_run_id','_created_at','_history_step','_store'):
        setattr(artifact,field,getattr(full,field))
    artifact._name=full._name
    object.__setattr__(artifact,'type',full.type)
    for field in ('metadata','description','tags'):
        if getattr(artifact,field)==handle.descriptive[field]:
            setattr(artifact,field,copy.deepcopy(getattr(full,field)))
    artifact._aliases=list(full._aliases)
    artifact._saved_aliases=list(full._saved_aliases)
    artifact._saved_descriptive=copy.deepcopy(full._saved_descriptive)
    artifact._owner=None;artifact._journal_count=None
    artifact._incremental_pending=False;artifact._distributed_pending=False
    artifact._logged=True;artifact._committed=True;artifact._contribution_handle=None
